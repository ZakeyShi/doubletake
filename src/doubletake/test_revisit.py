import os
from pathlib import Path

import pytorch_lightning as pl
import torch
import torch.nn.functional as F
from tqdm import tqdm

import doubletake.options as options
from doubletake.datasets.scannet_dataset import ScannetDataset
from doubletake.datasets.threer_scan_dataset import ThreeRScanDataset
from doubletake.test_offline_two_pass import compute_hint_mesh
from doubletake.tools import fusers_helper
from doubletake.utils.dataset_utils import get_dataset
from doubletake.utils.generic_utils import cache_model_outputs, to_gpu
from doubletake.utils.geometry_utils import BackprojectDepth
from doubletake.utils.metrics_utils import (
    ResultsAverager,
    compute_depth_metrics_batched,
)
from doubletake.utils.model_utils import get_model_class, load_model_inference
from doubletake.utils.rendering_utils import PyTorch3DMeshDepthRenderer
from doubletake.utils.visualization_utils import quick_viz_export


def main(opts):
    assert opts.dataset == "3rscan", "This script is only for 3RScan dataset."

    # get dataset
    dataset_class, scans = get_dataset(
        opts.dataset, opts.dataset_scan_split_file, opts.single_debug_scan_id
    )

    # path where results for this model, dataset, and tuple type are.
    results_path = os.path.join(
        opts.output_base_path, opts.name, opts.dataset, opts.frame_tuple_type
    )

    # set up directories for fusion
    if opts.run_fusion:
        mesh_output_folder_name = (
            f"{opts.fusion_resolution}_{opts.fusion_max_depth}_{opts.depth_fuser}"
        )

        if opts.mask_pred_depth:
            mesh_output_folder_name = mesh_output_folder_name + "_masked"
        if opts.fuse_color:
            mesh_output_folder_name = mesh_output_folder_name + "_color"

        mesh_output_dir = os.path.join(results_path, "meshes", mesh_output_folder_name)

        Path(mesh_output_dir).mkdir(parents=True, exist_ok=True)
        print(f"".center(80, "#"))
        print(f" Running fusion! Using {opts.depth_fuser} ".center(80, "#"))
        print(f"Output directory:\n{mesh_output_dir} ".center(80, "#"))
        print(f"".center(80, "#"))
        print("")

    # set up directories for caching depths
    if opts.cache_depths:
        # path where we cache depth maps
        depth_output_dir = os.path.join(results_path, "depths")

        Path(depth_output_dir).mkdir(parents=True, exist_ok=True)
        print(f"".center(80, "#"))
        print(f" Caching depths.".center(80, "#"))
        print(f"Output directory:\n{depth_output_dir} ".center(80, "#"))
        print(f"".center(80, "#"))
        print("")

    # set up directories for quick depth visualizations
    if opts.dump_depth_visualization:
        viz_output_folder_name = "quick_viz"
        viz_output_dir = os.path.join(results_path, "viz", viz_output_folder_name)

        Path(viz_output_dir).mkdir(parents=True, exist_ok=True)
        print(f"".center(80, "#"))
        print(f" Saving quick viz.".center(80, "#"))
        print(f"Output directory:\n{viz_output_dir} ".center(80, "#"))
        print(f"".center(80, "#"))
        print("")

    # set up directory for saving scores
    scores_output_dir = os.path.join(results_path, "scores")
    Path(scores_output_dir).mkdir(parents=True, exist_ok=True)

    # Set up model. Note that we're not passing in opts as an argument, although
    # we could. We're being pretty stubborn with using the options the model had
    # used when training, saved internally as part of hparams in the checkpoint.
    # You can change this at inference by passing in 'opts=opts,' but there
    # be dragons if you're not careful.

    model_class_to_use = get_model_class(opts)
    model = load_model_inference(opts, model_class_to_use)
    model = model.cuda().eval()

    # setting up overall result averagers
    all_frame_metrics = None
    all_scene_metrics = None

    all_frame_metrics = ResultsAverager(opts.name, f"frame metrics")
    all_scene_metrics = ResultsAverager(opts.name, f"scene metrics")

    rescan_map = dataset_class.parse_rescan_transforms(opts.dataset_path, scans)

    with torch.inference_mode():
        start_time = torch.cuda.Event(enable_timing=True)
        end_time = torch.cuda.Event(enable_timing=True)

        # loop over scans
        for scan in tqdm(scans):
            # set up dataset with a nighbor scan
            first_scan_id = list(rescan_map[scan].keys())[0]
            first_scan_T_second_scan_transform_144 = torch.inverse(
                torch.tensor(rescan_map[scan][first_scan_id]).unsqueeze(0)
            ).cuda()

            dataset = dataset_class(
                opts.dataset_path,
                split=opts.split,
                mv_tuple_file_suffix=opts.mv_tuple_file_suffix,
                limit_to_scan_id=first_scan_id,  # note the scan id
                include_full_res_depth=True,
                tuple_info_file_location=opts.tuple_info_file_location,
                num_images_in_tuple=None,
                shuffle_tuple=opts.shuffle_tuple,
                include_high_res_color=(
                    (opts.fuse_color and opts.run_fusion) or opts.dump_depth_visualization
                ),
                include_full_depth_K=True,
                skip_frames=opts.skip_frames,
                skip_to_frame=opts.skip_to_frame,
                image_width=opts.image_width,
                image_height=opts.image_height,
                pass_frame_id=True,
                fill_depth_hints=opts.fill_depth_hints,
                depth_hint_aug=opts.depth_hint_aug,
                depth_hint_dir=None,
                load_empty_hints=True,
                disable_flip=True,
                rotate_images=opts.rotate_images,
            )

            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=opts.batch_size,
                shuffle=False,
                num_workers=opts.num_workers,
                drop_last=False,
            )

            pytorch_hint_mesh, hint_fuser = compute_hint_mesh(opts, scan, dataloader, model)

            ######################### Run inference again with mesh hint. #########################
            # initialize scene averager
            dataset = dataset_class(
                opts.dataset_path,
                split=opts.split,
                mv_tuple_file_suffix=opts.mv_tuple_file_suffix,
                limit_to_scan_id=scan,
                include_full_res_depth=True,
                tuple_info_file_location=opts.tuple_info_file_location,
                num_images_in_tuple=None,
                shuffle_tuple=opts.shuffle_tuple,
                include_high_res_color=(
                    (opts.fuse_color and opts.run_fusion) or opts.dump_depth_visualization
                ),
                include_full_depth_K=True,
                skip_frames=opts.skip_frames,
                skip_to_frame=opts.skip_to_frame,
                image_width=opts.image_width,
                image_height=opts.image_height,
                pass_frame_id=True,
                fill_depth_hints=opts.fill_depth_hints,
                depth_hint_aug=opts.depth_hint_aug,
                depth_hint_dir=None,
                load_empty_hints=True,
                disable_flip=True,
                rotate_images=opts.rotate_images,
            )

            dataloader = torch.utils.data.DataLoader(
                dataset,
                batch_size=opts.batch_size,
                shuffle=False,
                num_workers=opts.num_workers,
                drop_last=False,
            )

            scene_frame_metrics = ResultsAverager(opts.name, f"scene {scan} metrics")

            if opts.run_fusion:
                fuser = fusers_helper.get_fuser(opts, scan)

            hint_start_time = torch.cuda.Event(enable_timing=True)
            hint_end_time = torch.cuda.Event(enable_timing=True)

            render_height = dataset.image_height // 2
            render_width = dataset.image_width // 2

            if opts.rotate_images:
                temp = render_height
                render_height = render_width
                render_width = temp

            backprojector = BackprojectDepth(height=render_height, width=render_width).cuda()
            mesh_renderer = PyTorch3DMeshDepthRenderer(height=render_height, width=render_width)

            for batch_ind, batch in enumerate(tqdm(dataloader, desc="Second pass")):
                # get data, move to GPU
                cur_data, src_data = batch
                cur_data = to_gpu(cur_data, key_ignores=["frame_id_string"])
                src_data = to_gpu(src_data, key_ignores=["frame_id_string"])

                depth_gt = cur_data["full_res_depth_b1hw"]

                # render hints and sample tsdf
                hint_start_time.record()

                # renderer expects normalized intrinsics.
                K_b44 = cur_data["K_s0_b44"].clone()
                K_b44[:, 0] /= render_width
                K_b44[:, 1] /= render_height

                first_scan_world_T_cam_b44 = (
                    first_scan_T_second_scan_transform_144 @ cur_data["world_T_cam_b44"]
                )
                cam_T_first_scan_world_b44 = torch.inverse(first_scan_world_T_cam_b44)

                rendered_depth_b1hw = mesh_renderer.render(
                    pytorch_hint_mesh, cam_T_first_scan_world_b44.clone(), K_b44
                )
                cur_data["depth_hint_b1hw"] = rendered_depth_b1hw.clone()
                cur_data["depth_hint_b1hw"][cur_data["depth_hint_b1hw"] == -1] = float("nan")
                cur_data["depth_hint_mask_b_b1hw"] = ~torch.isnan(cur_data["depth_hint_b1hw"])
                cur_data["depth_hint_mask_b1hw"] = cur_data["depth_hint_mask_b_b1hw"].float()

                cam_points_b4N = backprojector(rendered_depth_b1hw, cur_data["invK_s0_b44"])
                # transform to world
                world_points_b4N = first_scan_world_T_cam_b44 @ cam_points_b4N

                # sample tsdf
                sampled_weights_N_list = []
                for world_points_4N in world_points_b4N:
                    sampled_weights_N = hint_fuser.sample_tsdf(
                        world_points_4N[:3, :].transpose(0, 1),
                        what_to_sample="weights",
                    )
                    sampled_weights_N_list.append(sampled_weights_N)

                sampled_weights_b1hw = torch.stack(sampled_weights_N_list, 0).view(
                    cur_data["image_b3hw"].shape[0], 1, render_height, render_width
                )

                sampled_weights_b1hw[~cur_data["depth_hint_mask_b_b1hw"]] = 0.0
                cur_data["sampled_weights_b1hw"] = sampled_weights_b1hw

                hint_end_time.record()
                torch.cuda.synchronize()
                elapsed_hint_time = hint_start_time.elapsed_time(hint_end_time)

                # run to get output, also measure time
                start_time.record()
                # use unbatched (looping) matching encoder image forward passes
                # for numerically stable testing. If opts.fast_cost_volume, then
                # batch.
                outputs = model(
                    "test",
                    cur_data,
                    src_data,
                    unbatched_matching_encoder_forward=(not opts.fast_cost_volume),
                    return_mask=True,
                )
                end_time.record()
                torch.cuda.synchronize()

                elapsed_model_time = start_time.elapsed_time(end_time)

                upsampled_depth_pred_b1hw = F.interpolate(
                    outputs["depth_pred_s0_b1hw"],
                    size=(depth_gt.shape[-2], depth_gt.shape[-1]),
                    mode="nearest",
                )

                # inf max depth matches DVMVS metrics, using minimum of 0.5m
                valid_mask_b = cur_data["full_res_depth_b1hw"] > 0.5

                # Check if there any valid gt points in this sample
                if (valid_mask_b).any():
                    # compute metrics
                    metrics_b_dict = compute_depth_metrics_batched(
                        depth_gt.flatten(start_dim=1).float(),
                        upsampled_depth_pred_b1hw.flatten(start_dim=1).float(),
                        valid_mask_b.flatten(start_dim=1),
                        mult_a=True,
                    )

                    # go over batch and get metrics frame by frame to update
                    # the averagers
                    for element_index in range(depth_gt.shape[0]):
                        if (~valid_mask_b[element_index]).all():
                            # ignore if no valid gt exists
                            continue

                        element_metrics = {}
                        for key in list(metrics_b_dict.keys()):
                            element_metrics[key] = metrics_b_dict[key][element_index]

                        # get per frame time in the batch
                        element_metrics["model_time"] = elapsed_model_time / depth_gt.shape[0]

                        # get per frame time in the batch
                        element_metrics["model_time"] = elapsed_model_time / depth_gt.shape[0]
                        element_metrics["hint_time"] = elapsed_hint_time / depth_gt.shape[0]

                        # both this scene and all frame averagers
                        scene_frame_metrics.update_results(element_metrics)
                        all_frame_metrics.update_results(element_metrics)

                ######################### DEPTH FUSION #########################
                if opts.run_fusion:
                    # mask predicted depths when no vaiid MVS information
                    # exists, off by default
                    if opts.mask_pred_depth:
                        overall_mask_b1hw = outputs["overall_mask_bhw"].cuda().unsqueeze(1).float()

                        overall_mask_b1hw = F.interpolate(
                            overall_mask_b1hw,
                            size=(depth_gt.shape[-2], depth_gt.shape[-1]),
                            mode="nearest",
                        ).bool()

                        upsampled_depth_pred_b1hw[~overall_mask_b1hw] = -1

                    color_frame = (
                        cur_data["high_res_color_b3hw"]
                        if "high_res_color_b3hw" in cur_data
                        else cur_data["image_b3hw"]
                    )

                    fuser.fuse_frames(
                        upsampled_depth_pred_b1hw,
                        cur_data["K_full_depth_b44"],
                        cur_data["cam_T_world_b44"],
                        color_frame,
                    )

                ########################### Quick Viz ##########################
                if opts.dump_depth_visualization:
                    # make a dir for this scan
                    output_path = os.path.join(viz_output_dir, scan)
                    Path(output_path).mkdir(parents=True, exist_ok=True)

                    if "sampled_weights_b1hw" in cur_data:
                        outputs["sampled_weights_b1hw"] = cur_data["sampled_weights_b1hw"]
                    if "rendered_depth_b1hw" in cur_data:
                        outputs["rendered_depth_b1hw"] = cur_data["rendered_depth_b1hw"]

                    quick_viz_export(
                        output_path,
                        outputs,
                        cur_data,
                        batch_ind,
                        valid_mask_b,
                        opts.batch_size,
                    )
                ########################## Cache Depths ########################
                if opts.cache_depths:
                    output_path = os.path.join(depth_output_dir, scan)
                    Path(output_path).mkdir(parents=True, exist_ok=True)

                    cache_model_outputs(
                        output_path,
                        outputs,
                        cur_data,
                        src_data,
                        batch_ind,
                        opts.batch_size,
                    )

            # save the fused tsdf into a mesh file
            if opts.run_fusion:
                fuser.export_mesh(
                    os.path.join(mesh_output_dir, f"{scan.replace('/', '_')}.ply"),
                )
                fuser.save_tsdf(
                    os.path.join(mesh_output_dir, f"{scan.replace('/', '_')}_tsdf.npz"),
                )

            # compute a clean average
            scene_frame_metrics.compute_final_average()

            # one scene counts as a complete unit of metrics
            all_scene_metrics.update_results(scene_frame_metrics.final_metrics)

            # print running metrics.
            print("\nScene metrics:")
            scene_frame_metrics.print_sheets_friendly(include_metrics_names=True)
            scene_frame_metrics.output_json(
                os.path.join(scores_output_dir, f"{scan.replace('/', '_')}_metrics.json")
            )
            # print running metrics.
            print("\nRunning frame metrics:")
            all_frame_metrics.print_sheets_friendly(
                include_metrics_names=False,
                print_running_metrics=True,
            )

        # compute and print final average
        print("\nFinal metrics:")
        all_scene_metrics.compute_final_average()
        all_scene_metrics.pretty_print_results(print_running_metrics=False)
        all_scene_metrics.print_sheets_friendly(
            include_metrics_names=True,
            print_running_metrics=False,
        )
        all_scene_metrics.output_json(
            os.path.join(scores_output_dir, f"all_scene_avg_metrics_{opts.split}.json")
        )

        print("")
        all_frame_metrics.compute_final_average()
        all_frame_metrics.pretty_print_results(print_running_metrics=False)
        all_frame_metrics.print_sheets_friendly(
            include_metrics_names=True, print_running_metrics=False
        )
        all_frame_metrics.output_json(
            os.path.join(scores_output_dir, f"all_frame_avg_metrics_{opts.split}.json")
        )


if __name__ == "__main__":
    # don't need grad for test.
    torch.set_grad_enabled(False)
    pl.seed_everything(42)

    # get an instance of options and load it with config file(s) and cli args.
    option_handler = options.OptionsHandler()
    option_handler.parse_and_merge_options()
    option_handler.pretty_print_options()
    print("\n")
    opts = option_handler.options

    # if no GPUs are available for us then, use the 32 bit on CPU
    if opts.gpus == 0:
        print("Setting precision to 32 bits since --gpus is set to 0.")
        opts.precision = 32

    main(opts)
