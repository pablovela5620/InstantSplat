#
# Copyright (C) 2023, Inria
# GRAPHDECO research group, https://team.inria.fr/graphdeco
# All rights reserved.
#
# This software is free for non-commercial, research and evaluation use
# under the terms of the LICENSE.md file.
#
# For inquiries contact  george.drettakis@inria.fr
#

import os
import numpy as np
import torch
import rerun as rr
from random import randint
from instant_splat.scene.cameras import Camera
from instant_splat.utils.loss_utils import l1_loss, ssim
from instant_splat.gaussian_renderer import render
from instant_splat.utils.sh_utils import SH2RGB
import sys
from instant_splat.scene import Scene, GaussianModel
import uuid
from tqdm import tqdm
from instant_splat.utils.image_utils import psnr
from argparse import ArgumentParser, Namespace
from arguments import ModelParams, PipelineParams, OptimizationParams, GroupParams
from instant_splat.utils.pose_utils import get_camera_from_tensor
from torch import Tensor
from jaxtyping import Float32
from typing import Any
from pathlib import Path
from icecream import ic


from time import perf_counter

TENSORBOARD_FOUND = False


def save_pose(path, quat_pose, train_cams, llffhold=2):
    output_poses = []
    index_colmap = [cam.colmap_id for cam in train_cams]
    for quat_t in quat_pose:
        w2c = get_camera_from_tensor(quat_t)
        output_poses.append(w2c)
    colmap_poses = []
    for i in range(len(index_colmap)):
        ind = index_colmap.index(i + 1)
        bb = output_poses[ind]
        bb = bb  # .inverse()
        colmap_poses.append(bb)
    colmap_poses = torch.stack(colmap_poses).detach().cpu().numpy()
    np.save(path, colmap_poses)


def log_3d_splats(parent_log_path: Path, gaussians: GaussianModel):
    rr.log(f"{parent_log_path}", rr.ViewCoordinates.RDF, static=True)
    initial_gaussians: Float32[Tensor, "num_gaussians 3"] = gaussians.get_xyz
    scales = gaussians.get_scaling
    rotations = gaussians.get_rotation
    colors: Float32[Tensor, "num_gaussians 3"] = SH2RGB(gaussians.get_features)[:, 0, :]
    ic(scales.shape, scales.dtype)
    ic(rotations.shape, rotations.dtype)
    # get only the first 10 gaussians
    rr.log(
        f"{parent_log_path}/gaussian_points",
        rr.Points3D(
            positions=initial_gaussians.numpy(force=True),
            colors=colors.numpy(force=True),
        ),
    )
    # rr.log(
    #     f"{parent_log_path}/gaussian_ellipsoids",
    #     rr.Ellipsoids3D(
    #         centers=initial_gaussians.numpy(force=True),
    #         quaternions=rotations.numpy(force=True),
    #         half_sizes=scales.numpy(force=True),
    #         colors=colors.numpy(force=True),
    #         fill_mode=3,
    #     ),
    # )


def training(
    dataset: GroupParams,
    opt: GroupParams,
    pipe: GroupParams,
    testing_iterations: list[int],
    saving_iterations: list[int],
    checkpoint_iterations: list,
    checkpoint: None,
    debug_from: int,
    args: Namespace,
):
    first_iter = 0
    tb_writer = prepare_output_and_logger(dataset)
    gaussians = GaussianModel(dataset.sh_degree)
    scene = Scene(dataset, gaussians, opt=args, shuffle=True)
    gaussians.training_setup(opt)
    if checkpoint:
        (model_params, first_iter) = torch.load(checkpoint)
        gaussians.restore(model_params, opt)
    train_cams_init = scene.getTrainCameras().copy()
    os.makedirs(scene.model_path + "pose", exist_ok=True)
    save_pose(scene.model_path + "pose" + "/pose_org.npy", gaussians.P, train_cams_init)
    bg_color: list[int] = [1, 1, 1] if dataset.white_background else [0, 0, 0]
    background: Float32[Tensor, "3 "] = torch.tensor(
        bg_color, dtype=torch.float32, device="cuda"
    )

    iter_start = torch.cuda.Event(enable_timing=True)
    iter_end = torch.cuda.Event(enable_timing=True)

    viewpoint_stack = None
    ema_loss_for_log = 0.0
    progress_bar = tqdm(range(first_iter, opt.iterations), desc="Training progress")
    first_iter += 1

    start = perf_counter()
    parent_log_path = Path("world")

    # log initial camera positions

    for iteration in range(first_iter, opt.iterations + 1):
        iter_start.record()
        rr.set_time_sequence("Iteration", iteration)

        gaussians.update_learning_rate(iteration)

        if args.optim_pose is False:
            gaussians.P.requires_grad_(False)

        # Every 1000 its we increase the levels of SH up to a maximum degree
        if iteration % 1000 == 0:
            gaussians.oneupSHdegree()

        # Pick a random Camera
        if not viewpoint_stack:
            viewpoint_stack: list[Camera] = scene.getTrainCameras().copy()
        viewpoint_cam: Camera = viewpoint_stack.pop(
            randint(0, len(viewpoint_stack) - 1)
        )
        pose: Float32[Tensor, "7"] = gaussians.get_RT(viewpoint_cam.uid)

        for idx, quat_t in enumerate(gaussians.P):
            w2c = get_camera_from_tensor(quat_t)
            cam_T_world = w2c.numpy(force=True)
            cam_R_world = cam_T_world[:3, :3]
            cam_t_world = cam_T_world[:3, 3]

            rr.log(
                f"{parent_log_path}/camera_{idx}",
                rr.Transform3D(
                    translation=cam_t_world, mat3x3=cam_R_world, from_parent=True
                ),
            )

        # Render
        if (iteration - 1) == debug_from:
            pipe.debug = True

        bg: Float32[Tensor, "3"] = (
            torch.rand((3), device="cuda") if opt.random_background else background
        )

        render_pkg: dict[str, Any] = render(
            viewpoint_cam, gaussians, pipe, bg, camera_pose=pose
        )
        image: Float32[Tensor, "c h w"] = render_pkg["render"]
        viewspace_point_tensor: Float32[Tensor, "num_gaussians 3"] = render_pkg[
            "viewspace_points"
        ]
        visibility_filter: Float32[Tensor, "num_gaussians"] = render_pkg[  # noqa : F841
            "visibility_filter"
        ]
        radii: Float32[Tensor, "num_gaussians"] = render_pkg["radii"]  # noqa : F841

        # Loss
        gt_image: Float32[Tensor, "c h w"] = viewpoint_cam.original_image.cuda()

        img_vis = image.permute(1, 2, 0) * 255
        rr.log(
            f"{parent_log_path}/image_{viewpoint_cam.uid}",
            rr.Image(img_vis.numpy(force=True).astype(np.uint8)).compress(
                jpeg_quality=50
            ),
        )
        img_gt_vis = gt_image.permute(1, 2, 0) * 255
        rr.log(
            f"{parent_log_path}/gt_image_{viewpoint_cam.uid}",
            rr.Image(img_gt_vis.numpy(force=True).astype(np.uint8)).compress(
                jpeg_quality=50
            ),
        )
        Ll1 = l1_loss(image, gt_image)
        loss = (1.0 - opt.lambda_dssim) * Ll1 + opt.lambda_dssim * (
            1.0 - ssim(image, gt_image)
        )
        loss.backward()

        iter_end.record()

        with torch.no_grad():
            # Progress bar
            ema_loss_for_log = 0.4 * loss.item() + 0.6 * ema_loss_for_log
            if iteration % 10 == 0:
                progress_bar.set_postfix({"Loss": f"{ema_loss_for_log:.{7}f}"})
                progress_bar.update(10)
            if iteration == opt.iterations:
                progress_bar.close()

            # Log and save
            training_report(
                tb_writer,
                iteration,
                Ll1,
                loss,
                l1_loss,
                iter_start.elapsed_time(iter_end),
                testing_iterations,
                scene,
                render,
                (pipe, background),
            )
            if iteration in saving_iterations:
                print("\n[ITER {}] Saving Gaussians".format(iteration))
                scene.save(iteration)
                save_pose(
                    scene.model_path + "pose" + f"/pose_{iteration}.npy",
                    gaussians.P,
                    train_cams_init,
                )

            if iteration % 100 == 0 or iteration == 1:
                log_3d_splats(parent_log_path, gaussians)

            # Optimizer step
            if iteration < opt.iterations:
                gaussians.optimizer.step()
                gaussians.optimizer.zero_grad(set_to_none=True)

            if iteration in checkpoint_iterations:
                print("\n[ITER {}] Saving Checkpoint".format(iteration))
                torch.save(
                    (gaussians.capture(), iteration),
                    scene.model_path + "/chkpnt" + str(iteration) + ".pth",
                )

        end = perf_counter()
        train_time = end - start


def prepare_output_and_logger(args):
    if not args.model_path:
        if os.getenv("OAR_JOB_ID"):
            unique_str = os.getenv("OAR_JOB_ID")
        else:
            unique_str = str(uuid.uuid4())
        args.model_path = os.path.join("./output/", unique_str[0:10])

    # Set up output folder
    print("Output folder: {}".format(args.model_path))
    os.makedirs(args.model_path, exist_ok=True)
    with open(os.path.join(args.model_path, "cfg_args"), "w") as cfg_log_f:
        cfg_log_f.write(str(Namespace(**vars(args))))

    # Create Tensorboard writer
    tb_writer = None
    if TENSORBOARD_FOUND:
        tb_writer = SummaryWriter(args.model_path)
    else:
        print("Tensorboard not available: not logging progress")
    return tb_writer


def training_report(
    tb_writer,
    iteration,
    Ll1,
    loss,
    l1_loss,
    elapsed,
    testing_iterations,
    scene: Scene,
    renderFunc,
    renderArgs,
):
    if tb_writer:
        tb_writer.add_scalar("train_loss_patches/l1_loss", Ll1.item(), iteration)
        tb_writer.add_scalar("train_loss_patches/total_loss", loss.item(), iteration)
        tb_writer.add_scalar("iter_time", elapsed, iteration)

    # Report test and samples of training set
    if iteration in testing_iterations:
        torch.cuda.empty_cache()
        validation_configs = (
            {"name": "test", "cameras": scene.getTestCameras()},
            {
                "name": "train",
                "cameras": [
                    scene.getTrainCameras()[idx % len(scene.getTrainCameras())]
                    for idx in range(len(scene.getTrainCameras()))
                ],
            },
        )

        for config in validation_configs:
            if config["cameras"] and len(config["cameras"]) > 0:
                l1_test = 0.0
                psnr_test = 0.0
                for idx, viewpoint in enumerate(config["cameras"]):
                    if config["name"] == "train":
                        pose = scene.gaussians.get_RT(viewpoint.uid)
                    else:
                        pose = scene.gaussians.get_RT_test(viewpoint.uid)
                    image = torch.clamp(
                        renderFunc(
                            viewpoint, scene.gaussians, *renderArgs, camera_pose=pose
                        )["render"],
                        0.0,
                        1.0,
                    )
                    gt_image = torch.clamp(
                        viewpoint.original_image.to("cuda"), 0.0, 1.0
                    )
                    if tb_writer and (idx < 5):
                        tb_writer.add_images(
                            config["name"]
                            + "_view_{}/render".format(viewpoint.image_name),
                            image[None],
                            global_step=iteration,
                        )
                        if iteration == testing_iterations[0]:
                            tb_writer.add_images(
                                config["name"]
                                + "_view_{}/ground_truth".format(viewpoint.image_name),
                                gt_image[None],
                                global_step=iteration,
                            )
                    l1_test += l1_loss(image, gt_image).mean().double()
                    psnr_test += psnr(image, gt_image).mean().double()
                psnr_test /= len(config["cameras"])
                l1_test /= len(config["cameras"])
                print(
                    "\n[ITER {}] Evaluating {}: L1 {} PSNR {}".format(
                        iteration, config["name"], l1_test, psnr_test
                    )
                )
                if tb_writer:
                    tb_writer.add_scalar(
                        config["name"] + "/loss_viewpoint - l1_loss", l1_test, iteration
                    )
                    tb_writer.add_scalar(
                        config["name"] + "/loss_viewpoint - psnr", psnr_test, iteration
                    )

        if tb_writer:
            tb_writer.add_histogram(
                "scene/opacity_histogram", scene.gaussians.get_opacity, iteration
            )
            tb_writer.add_scalar(
                "total_points", scene.gaussians.get_xyz.shape[0], iteration
            )
        torch.cuda.empty_cache()


if __name__ == "__main__":
    # Set up command line argument parser
    parser = ArgumentParser(description="Training script parameters")
    lp = ModelParams(parser)
    op = OptimizationParams(parser)
    pp = PipelineParams(parser)
    parser.add_argument("--ip", type=str, default="127.0.0.1")
    parser.add_argument("--port", type=int, default=6009)
    parser.add_argument("--debug_from", type=int, default=-1)
    parser.add_argument("--detect_anomaly", action="store_true", default=False)
    parser.add_argument(
        "--test_iterations",
        nargs="+",
        type=int,
        default=[500, 800, 1000, 1500, 2000, 3000, 4000, 5000, 6000, 7_000, 30_000],
    )
    parser.add_argument("--save_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--quiet", action="store_true")
    parser.add_argument("--checkpoint_iterations", nargs="+", type=int, default=[])
    parser.add_argument("--start_checkpoint", type=str, default=None)
    parser.add_argument("--scene", type=str, default=None)
    parser.add_argument("--n_views", type=int, default=None)
    parser.add_argument("--get_video", action="store_true")
    parser.add_argument("--optim_pose", action="store_true")
    rr.script_add_args(parser)
    args = parser.parse_args(sys.argv[1:])
    args.save_iterations.append(args.iterations)

    rr.script_setup(args, "train_joint")

    os.makedirs(args.model_path, exist_ok=True)

    print("Optimizing " + args.model_path)

    # Initialize system state (RNG)
    # safe_state(args.quiet)

    # Start GUI server, configure and run training
    # network_gui.init(args.ip, args.port)
    torch.autograd.set_detect_anomaly(args.detect_anomaly)
    training(
        lp.extract(args),
        op.extract(args),
        pp.extract(args),
        args.test_iterations,
        args.save_iterations,
        args.checkpoint_iterations,
        args.start_checkpoint,
        args.debug_from,
        args,
    )

    # All done
    print("\nTraining complete.")
    rr.script_teardown(args)
