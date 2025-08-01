# Mikel Broström 🔥 Yolo Tracking 🧾 AGPL-3.0 license

import multiprocessing as mp
mp.set_start_method("spawn", force=True)

import argparse
import subprocess
from pathlib import Path
import numpy as np
from tqdm import tqdm
import configparser
import shutil
import json
import yaml
import cv2
import re
import os
import torch
import threading
import sys
import copy
import concurrent.futures

from boxmot.tracker_zoo import create_tracker
from boxmot.utils import NUM_THREADS, ROOT, WEIGHTS, TRACKER_CONFIGS, DATASET_CONFIGS, logger as LOGGER, TRACKEVAL
from boxmot.utils.checks import RequirementsChecker
from boxmot.utils.torch_utils import select_device
from boxmot.utils.plots import MetricsPlotter
from boxmot.utils.misc import increment_path, prompt_overwrite
from boxmot.utils.clean import cleanup_mot17
from typing import Optional, List, Dict, Generator, Union

from boxmot.utils.dataloaders.MOT17 import MOT17DetEmbDataset
from boxmot.postprocessing.gsi import gsi

from ultralytics import YOLO
from ultralytics.data.build import load_inference_source

from boxmot.engine.detectors import (get_yolo_inferer, default_imgsz,
                                is_ultralytics_model, is_yolox_model)
from boxmot.engine.utils import convert_to_mot_format, write_mot_results
from boxmot.appearance.reid.auto_backend import ReidAutoBackend
from tqdm import tqdm
from boxmot.utils.download import download_eval_data, download_trackeval

checker = RequirementsChecker()
checker.check_packages(('ultralytics', ))  # install


def eval_init(args,
              trackeval_dest: Path = TRACKEVAL,
              branch: str = "master",
              overwrite: bool = False
    ) -> None:
    """
    Common initialization: download TrackEval and (if needed) the MOT-challenge
    data for ablation runs, then canonicalize args.source.
    Modifies args in place.
    """
    # 1) download the TrackEval code
    download_trackeval(dest=trackeval_dest, branch=branch, overwrite=overwrite)

    # 2) if doing MOT17/20-ablation, pull down the dataset and rewire args.source/split
    if args.source in ("MOT17-ablation", "MOT20-ablation", "dancetrack-ablation", "vizdrone-ablation"):
        cfg = load_dataset_cfg(str(args.source))
        download_eval_data(
            runs_url=cfg["download"]["runs_url"],
            dataset_url=cfg["download"]["dataset_url"],
            dataset_dest=Path(cfg["download"]["dataset_dest"]),
            benchmark = cfg["benchmark"]["name"],
            overwrite=overwrite
        )
        args.benchmark = cfg["benchmark"]["name"]
        args.split = cfg["benchmark"]["split"]
        args.source = TRACKEVAL / f"data/{args.benchmark}/{args.split}"
        

    # 3) finally, make source an absolute Path everywhere
    args.source = Path(args.source).resolve()


def generate_dets_embs(args: argparse.Namespace, y: Path, source: Path) -> None:
    """
    Generates detections and embeddings for the specified 
    arguments, YOLO model and source.

    Args:
        args (Namespace): Parsed command line arguments.
        y (Path): Path to the YOLO model file.
        source (Path): Path to the source directory.
    """
    WEIGHTS.mkdir(parents=True, exist_ok=True)

    if args.imgsz is None:
        args.imgsz = default_imgsz(y)

    yolo = YOLO(
        y if is_ultralytics_model(y)
        else 'yolov8n.pt',
    )

    results = yolo(
        source=source,
        conf=args.conf,
        iou=args.iou,
        agnostic_nms=args.agnostic_nms,
        stream=True,
        device=args.device,
        verbose=False,
        exist_ok=args.exist_ok,
        project=args.project,
        name=args.name,
        classes=args.classes,
        imgsz=args.imgsz,
        vid_stride=args.vid_stride,
    )

    if not is_ultralytics_model(y):
        m = get_yolo_inferer(y)
        yolo_model = m(model=y, device=yolo.predictor.device,
                       args=yolo.predictor.args)
        yolo.predictor.model = yolo_model

        # If current model is YOLOX, change the preprocess and postprocess
        if is_yolox_model(y):
            # add callback to save image paths for further processing
            yolo.add_callback("on_predict_batch_start",
                              lambda p: yolo_model.update_im_paths(p))
            yolo.predictor.preprocess = (
                lambda im: yolo_model.preprocess(im=im))
            yolo.predictor.postprocess = (
                lambda preds, im, im0s:
                yolo_model.postprocess(preds=preds, im=im, im0s=im0s))

    reids = []
    for r in args.reid_model:
        reid_model = ReidAutoBackend(weights=args.reid_model,
                                     device=yolo.predictor.device,
                                     half=args.half).model
        reids.append(reid_model)
        embs_path = args.project / 'dets_n_embs' / y.stem / 'embs' / r.stem / (source.parent.name + '.txt')
        embs_path.parent.mkdir(parents=True, exist_ok=True)
        embs_path.touch(exist_ok=True)

        if os.path.getsize(embs_path) > 0:
            open(embs_path, 'w').close()

    yolo.predictor.custom_args = args

    dets_path = args.project / 'dets_n_embs' / y.stem / 'dets' / (source.parent.name + '.txt')
    dets_path.parent.mkdir(parents=True, exist_ok=True)
    dets_path.touch(exist_ok=True)

    if os.path.getsize(dets_path) > 0:
        open(dets_path, 'w').close()

    with open(str(dets_path), 'ab+') as f:
        np.savetxt(f, [], fmt='%f', header=str(source))

    for frame_idx, r in enumerate(tqdm(results, desc="Frames")):
        nr_dets = len(r.boxes)
        frame_idx = torch.full((1, 1), frame_idx + 1).repeat(nr_dets, 1)
        img = r.orig_img

        dets = np.concatenate(
            [
                frame_idx,
                r.boxes.xyxy.to('cpu'),
                r.boxes.conf.unsqueeze(1).to('cpu'),
                r.boxes.cls.unsqueeze(1).to('cpu'),
            ], axis=1
        )

        # Filter dets with incorrect boxes: (x2 < x1 or y2 < y1)
        boxes = r.boxes.xyxy.to('cpu').numpy().round().astype(int)
        boxes_filter = ((np.maximum(0, boxes[:, 0]) < np.minimum(boxes[:, 2], img.shape[1])) &
                        (np.maximum(0, boxes[:, 1]) < np.minimum(boxes[:, 3], img.shape[0])))
        dets = dets[boxes_filter]

        with open(str(dets_path), 'ab+') as f:
            np.savetxt(f, dets, fmt='%f')

        for reid, reid_model_name in zip(reids, args.reid_model):
            embs = reid.get_features(dets[:, 1:5], img)
            embs_path = args.project / "dets_n_embs" / y.stem / 'embs' / reid_model_name.stem / (source.parent.name + '.txt')
            with open(str(embs_path), 'ab+') as f:
                np.savetxt(f, embs, fmt='%f')


def parse_mot_results(results: str) -> dict:
    """
    Extracts COMBINED HOTA, MOTA, IDF1, AssA, AssRe, IDSW, and IDs from MOTChallenge evaluation output.

    Args:
        results (str): Full MOT evaluation output as a string.

    Returns:
        dict: Dictionary with parsed metrics.
    """
    metric_specs = {
        'HOTA':   ('HOTA:',      {'HOTA': 0, 'AssA': 2, 'AssRe': 5}),
        'MOTA':   ('CLEAR:',     {'MOTA': 0, 'IDSW': 12}),
        'IDF1':   ('Identity:',  {'IDF1': 0}),
        'IDs':    ('Count:',     {'IDs': 2}),
    }

    int_fields = {'IDSW', 'IDs'}
    metrics = {}

    for section, fields_map in metric_specs.values():
        match = re.search(fr'{re.escape(section)}.*?COMBINED\s+(.*?)\n', results, re.DOTALL)
        if match:
            fields = match.group(1).split()
            for key, idx in fields_map.items():
                if idx < len(fields):
                    value = fields[idx]
                    metrics[key] = int(value) if key in int_fields else float(value)

    return metrics




def trackeval(args: argparse.Namespace, seq_paths: list, save_dir: Path, gt_folder: Path, metrics: list = ["HOTA", "CLEAR", "Identity"]) -> str:
    """
    Executes a Python script to evaluate MOT challenge tracking results using specified metrics.

    Args:
        seq_paths (list): List of sequence paths.
        save_dir (Path): Directory to save evaluation results.
        gt_folder (Path): Folder containing ground truth data.
        metrics (list, optional): List of metrics to use for evaluation. Defaults to ["HOTA", "CLEAR", "Identity"].

    Returns:
        str: Standard output from the evaluation script.
    """

    d = [seq_path.parent.name for seq_path in seq_paths]

    args = [
        sys.executable, TRACKEVAL / 'scripts' / 'run_mot_challenge.py',
        "--GT_FOLDER", str(gt_folder),
        "--BENCHMARK", "",
        "--TRACKERS_FOLDER", args.exp_dir,
        "--TRACKERS_TO_EVAL", "",
        "--SPLIT_TO_EVAL", args.split,
        "--METRICS", *metrics,
        "--USE_PARALLEL", "True",
        "--TRACKER_SUB_FOLDER", "",
        "--NUM_PARALLEL_CORES", str(4),
        "--SKIP_SPLIT_FOL", "True",
        "--GT_LOC_FORMAT", "{gt_folder}/{seq}/gt/gt_temp.txt",
        "--SEQ_INFO", *d
    ]

    p = subprocess.Popen(
        args=args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True
    )

    stdout, stderr = p.communicate()

    if stderr:
        print("Standard Error:\n", stderr)
    return stdout


def process_single_det_emb(y: Path, source_path: Path, opt: argparse.Namespace):
    new_opt = copy.deepcopy(opt)
    generate_dets_embs(new_opt, y, source=source_path / 'img1')

def run_generate_dets_embs(opt: argparse.Namespace) -> None:
    mot_folder_paths = sorted([item for item in Path(opt.source).iterdir()])

    for y in opt.yolo_model:
        dets_folder = Path(opt.project) / 'dets_n_embs' / y.stem / 'dets'
        embs_folder = Path(opt.project) / 'dets_n_embs' / y.stem / 'embs' / opt.reid_model[0].stem

        # Filter out already processed sequences
        tasks = []
        for i, mot_folder_path in enumerate(mot_folder_paths):
            dets_path = dets_folder / (mot_folder_path.name + '.txt')
            embs_path = embs_folder / (mot_folder_path.name + '.txt')
            if dets_path.exists() and embs_path.exists():
                if not prompt_overwrite('Detections and Embeddings', dets_path, opt.ci):
                    LOGGER.debug(f"Skipping generation for {mot_folder_path} as they already exist.")
                    continue
            tasks.append((y, mot_folder_path))

        LOGGER.info(f"Generating detections and embeddings for {len(tasks)} sequences with model {y.name}")

        with concurrent.futures.ProcessPoolExecutor(max_workers=NUM_THREADS) as executor:
            futures = [executor.submit(process_single_det_emb, y, source_path, opt) for y, source_path in tasks]

            for fut in concurrent.futures.as_completed(futures):
                try:
                    fut.result()
                except Exception as exc:
                    LOGGER.error(f"Error in det/emb task: {exc}")


def process_sequence(seq_name: str,
                     mot_root: str,
                     project_root: str,
                     model_name: str,
                     reid_name: str,
                     tracking_method: str,
                     exp_folder: str,
                     target_fps: Optional[int],
                     device: str,
                     cfg_dict: Optional[Dict] = None,
                     ):

    device = select_device(device)
    tracker = create_tracker(
        tracker_type=tracking_method,
        tracker_config=TRACKER_CONFIGS / (tracking_method + ".yaml"),
        reid_weights=Path(reid_name + '.pt'),
        device=device,
        half=False,
        per_class=False,
        evolve_param_dict=cfg_dict,
    )

    # load with the user’s FPS
    dataset = MOT17DetEmbDataset(
        mot_root=mot_root,
        det_emb_root=str(Path(project_root) / 'dets_n_embs'),
        model_name=model_name,
        reid_name=reid_name,
        target_fps=target_fps
    )
    sequence = dataset.get_sequence(seq_name)

    all_tracks = []
    kept_frame_ids = []
    for frame in sequence:
        fid  = int(frame['frame_id'])
        dets = frame['dets']
        embs = frame['embs']
        img  = frame['img']

        kept_frame_ids.append(fid)

        if dets.size and embs.size:
            tracks = tracker.update(dets, img, embs)
            if tracks.size:
                all_tracks.append(convert_to_mot_format(tracks, fid))

    out_arr = np.vstack(all_tracks) if all_tracks else np.empty((0, 0))
    write_mot_results(Path(exp_folder) / f"{seq_name}.txt", out_arr)
    return seq_name, kept_frame_ids


def run_generate_mot_results(opt: argparse.Namespace, evolve_config: dict = None) -> None:
    # Prepare experiment folder
    base = opt.project / 'mot' / f"{opt.yolo_model[0].stem}_{opt.reid_model[0].stem}_{opt.tracking_method}"
    exp_dir = increment_path(base, sep="_", exist_ok=False)
    exp_dir.mkdir(parents=True, exist_ok=True)
    opt.exp_dir = exp_dir

    # Just collect sequence names by scanning directory names
    sequence_names = sorted([
        d.name for d in Path(opt.source).iterdir()
        if d.is_dir() and (d / "img1").exists()
    ])

    # Build task arguments
    task_args = [
        (
            seq,
            str(opt.source),
            str(opt.project),
            opt.yolo_model[0].stem,
            opt.reid_model[0].stem,
            opt.tracking_method,
            str(exp_dir),
            getattr(opt, 'fps', None),
            opt.device,
            evolve_config,
        )
        for seq in sequence_names
    ]

    seq_frame_nums = {}

    with concurrent.futures.ProcessPoolExecutor(max_workers=NUM_THREADS) as executor:
        futures = {
            executor.submit(process_sequence, *args): args[0] for args in task_args
        }

        for fut in concurrent.futures.as_completed(futures):
            seq = futures[fut]
            try:
                seq_name, kept_ids = fut.result()
                seq_frame_nums[seq_name] = kept_ids
            except Exception as e:
                LOGGER.error(f"Error processing {seq}: {e}")

    # Optional GSI
    if getattr(opt, 'gsi', False):
        from boxmot.postprocessing.gsi import gsi
        gsi(mot_results_folder=exp_dir)


def run_trackeval(opt: argparse.Namespace) -> dict:
    """
    Runs the trackeval function to evaluate tracking results.

    Args:
        opt (Namespace): Parsed command line arguments.
    """
    gt_folder = opt.source
    seq_paths = [p / "img1" for p in opt.source.iterdir() if p.is_dir()]
    save_dir = Path(opt.project) / opt.name
    
    trackeval_results = trackeval(opt, seq_paths, save_dir, gt_folder)
    hota_mota_idf1 = parse_mot_results(trackeval_results)
    if opt.ci:
        with open(opt.tracking_method + "_output.json", "w") as outfile:
            outfile.write(json.dumps(hota_mota_idf1))
    LOGGER.info(trackeval_results)
    LOGGER.info(json.dumps(hota_mota_idf1))
    return hota_mota_idf1


def load_dataset_cfg(name: str) -> dict:
    """Load the dict from boxmot/configs/datasets/{name}.yaml."""
    path = DATASET_CONFIGS / f"{name}.yaml"
    with open(path, 'r') as f:
        return yaml.safe_load(f)


def main(args):
    # Download TrackEval
    eval_init(args)

    run_generate_dets_embs(args)
    run_generate_mot_results(args)
    results = run_trackeval(args)
    
    plotter = MetricsPlotter(args.exp_dir)

    plotter.plot_radar_chart(
        {args.tracking_method: list(results.values())},
        list(results.keys()),
        title="MOT metrics radar Chart",
        ylim=(65, 85),
        yticks=[65, 70, 75, 80, 85],
        ytick_labels=['65', '70', '75', '80', '85']
    )
        

if __name__ == "__main__":
    main()
