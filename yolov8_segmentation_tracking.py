import os
import cv2
import json
import subprocess
import numpy as np
from ultralytics import YOLO
from ultralytics.yolo.engine.results import Results
from collections import deque
from deep_sort_realtime.deepsort_tracker import DeepSort
from stqdm import stqdm
import streamlit as st
from streamlit_webrtc import WebRtcMode, webrtc_streamer, VideoTransformerBase
from typing import List, Tuple
import av


# colors for visualization for image visualization
COLORS = [
    (56, 56, 255),
    (151, 157, 255),
    (31, 112, 255),
    (29, 178, 255),
    (49, 210, 207),
    (10, 249, 72),
    (23, 204, 146),
    (134, 219, 61),
    (52, 147, 26),
    (187, 212, 0),
    (168, 153, 44),
    (255, 194, 0),
    (147, 69, 52),
    (255, 115, 100),
    (236, 24, 0),
    (255, 56, 132),
    (133, 0, 82),
    (255, 56, 203),
    (200, 149, 255),
    (199, 55, 255),
]


def process_frame(frame: av.VideoFrame) -> Tuple[av.VideoFrame, List]:
    # Convert the frame to a numpy array
    image = frame.to_ndarray(format="bgr24")

    # Process the image using the YOLOv8 model
    image, result_list_json = image_processing(
        image, model, tracker=tracker, centers=centers
    )

    # Convert the processed image back to an av.VideoFrame object
    processed_frame = av.VideoFrame.from_ndarray(image, format="bgr24")

    return processed_frame, result_list_json


def result_to_json(result: Results, tracker=None):
    """
    Convert result from ultralytics YOLOv8 prediction to json format
    Parameters:
        result: Results from ultralytics YOLOv8 prediction
        tracker: DeepSort tracker
    Returns:
        result_list_json: detection result in json format
    """
    len_results = len(result.boxes)
    result_list_json = [
        {
            "class_id": int(result.boxes.cls[idx]),
            "class": result.names[int(result.boxes.cls[idx])],
            "confidence": float(result.boxes.conf[idx]),
            "bbox": {
                "x_min": int(result.boxes.boxes[idx][0]),
                "y_min": int(result.boxes.boxes[idx][1]),
                "x_max": int(result.boxes.boxes[idx][2]),
                "y_max": int(result.boxes.boxes[idx][3]),
            },
        }
        for idx in range(len_results)
    ]
    if result.masks is not None:
        for idx in range(len_results):
            result_list_json[idx]["mask"] = cv2.resize(
                result.masks.data[idx].cpu().numpy(),
                (result.orig_shape[1], result.orig_shape[0]),
            ).tolist()
            result_list_json[idx]["segments"] = result.masks.segments[idx].tolist()
    if tracker is not None:
        bbs = [
            (
                [
                    result_list_json[idx]["bbox"]["x_min"],
                    result_list_json[idx]["bbox"]["y_min"],
                    result_list_json[idx]["bbox"]["x_max"]
                    - result_list_json[idx]["bbox"]["x_min"],
                    result_list_json[idx]["bbox"]["y_max"]
                    - result_list_json[idx]["bbox"]["y_min"],
                ],
                result_list_json[idx]["confidence"],
                result_list_json[idx]["class"],
            )
            for idx in range(len_results)
        ]
        tracks = tracker.update_tracks(bbs, frame=result.orig_img)
        for idx in range(len(result_list_json)):
            track_idx = next(
                (
                    i
                    for i, track in enumerate(tracks)
                    if track.det_conf is not None
                    and np.isclose(track.det_conf, result_list_json[idx]["confidence"])
                ),
                -1,
            )
            if track_idx != -1:
                result_list_json[idx]["object_id"] = int(tracks[track_idx].track_id)
    return result_list_json


def view_result_ultralytics(result: Results, result_list_json, centers=None):
    """
    Visualize result from ultralytics YOLOv8 prediction using ultralytics YOLOv8 built-in visualization function
    Parameters:
        result: Results from ultralytics YOLOv8 prediction
        result_list_json: detection result in json format
        centers: list of deque of center points of bounding boxes
    Returns:
        result_image_ultralytics: result image from ultralytics YOLOv8 built-in visualization function
    """
    result_image_ultralytics = result.plot()
    for result_json in result_list_json:
        class_color = COLORS[result_json["class_id"] % len(COLORS)]
        if "object_id" in result_json and centers is not None:
            centers[result_json["object_id"]].append(
                (
                    int(
                        (result_json["bbox"]["x_min"] + result_json["bbox"]["x_max"])
                        / 2
                    ),
                    int(
                        (result_json["bbox"]["y_min"] + result_json["bbox"]["y_max"])
                        / 2
                    ),
                )
            )
            for j in range(1, len(centers[result_json["object_id"]])):
                if (
                    centers[result_json["object_id"]][j - 1] is None
                    or centers[result_json["object_id"]][j] is None
                ):
                    continue
                thickness = int(np.sqrt(64 / float(j + 1)) * 2)
                cv2.line(
                    result_image_ultralytics,
                    centers[result_json["object_id"]][j - 1],
                    centers[result_json["object_id"]][j],
                    class_color,
                    thickness,
                )
    return result_image_ultralytics


def view_result_default(result: Results, result_list_json, centers=None):
    """
    Visualize result from ultralytics YOLOv8 prediction using default visualization function
    Parameters:
        result: Results from ultralytics YOLOv8 prediction
        result_list_json: detection result in json format
        centers: list of deque of center points of bounding boxes
    Returns:
        result_image_default: result image from default visualization function
    """
    ALPHA = 0.5
    image = result.orig_img
    for result in result_list_json:
        class_color = COLORS[result["class_id"] % len(COLORS)]
        if "mask" in result:
            image_mask = np.stack(
                [
                    np.array(result["mask"]) * class_color[0],
                    np.array(result["mask"]) * class_color[1],
                    np.array(result["mask"]) * class_color[2],
                ],
                axis=-1,
            ).astype(np.uint8)
            image = cv2.addWeighted(image, 1, image_mask, ALPHA, 0)
        text = (
            f"{result['class']} {result['object_id']}: {result['confidence']:.2f}"
            if "object_id" in result
            else f"{result['class']}: {result['confidence']:.2f}"
        )
        cv2.rectangle(
            image,
            (result["bbox"]["x_min"], result["bbox"]["y_min"]),
            (result["bbox"]["x_max"], result["bbox"]["y_max"]),
            class_color,
            2,
        )
        (text_width, text_height), baseline = cv2.getTextSize(
            text, cv2.FONT_HERSHEY_SIMPLEX, 0.75, 2
        )
        cv2.rectangle(
            image,
            (result["bbox"]["x_min"], result["bbox"]["y_min"] - text_height - baseline),
            (result["bbox"]["x_min"] + text_width, result["bbox"]["y_min"]),
            class_color,
            -1,
        )
        cv2.putText(
            image,
            text,
            (result["bbox"]["x_min"], result["bbox"]["y_min"] - baseline),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (255, 255, 255),
            2,
        )
        if "object_id" in result and centers is not None:
            centers[result["object_id"]].append(
                (
                    int((result["bbox"]["x_min"] + result["bbox"]["x_max"]) / 2),
                    int((result["bbox"]["y_min"] + result["bbox"]["y_max"]) / 2),
                )
            )
            for j in range(1, len(centers[result["object_id"]])):
                if (
                    centers[result["object_id"]][j - 1] is None
                    or centers[result["object_id"]][j] is None
                ):
                    continue
                thickness = int(np.sqrt(64 / float(j + 1)) * 2)
                cv2.line(
                    image,
                    centers[result["object_id"]][j - 1],
                    centers[result["object_id"]][j],
                    class_color,
                    thickness,
                )
    return image


def image_processing(
    frame, model, image_viewer=view_result_default, tracker=None, centers=None
):
    """
    Process image frame using ultralytics YOLOv8 model and possibly DeepSort tracker if it is provided
    Parameters:
        frame: image frame
        model: ultralytics YOLOv8 model
        image_viewer: function to visualize result, default is view_result_default, can be view_result_ultralytics
        tracker: DeepSort tracker
        centers: list of deque of center points of bounding boxes
    Returns:
        result_image: result image with bounding boxes, class names, confidence scores, object masks, and possibly object IDs
        result_list_json: detection result in json format
    """
    results = model.predict(frame)
    result_list_json = result_to_json(results[0], tracker=tracker)
    result_image = image_viewer(results[0], result_list_json, centers=centers)
    return result_image, result_list_json


def video_processing(
    video_file, model, image_viewer=view_result_default, tracker=None, centers=None
):
    """
    Process video file using ultralytics YOLOv8 model and possibly DeepSort tracker if it is provided
    Parameters:
        video_file: video file
        model: ultralytics YOLOv8 model
        image_viewer: function to visualize result, default is view_result_default, can be view_result_ultralytics
        tracker: DeepSort tracker
        centers: list of deque of center points of bounding boxes
    Returns:
        video_file_name_out: name of output video file
        result_video_json_file: file containing detection result in json format
    """
    results = model.predict(video_file)
    model_name = model.ckpt_path.split("/")[-1].split(".")[0]
    output_folder = os.path.join("output_videos", video_file.split(".")[0])
    if not os.path.exists(output_folder):
        os.makedirs(output_folder)
    video_file_name_out = os.path.join(
        output_folder, f"{video_file.split('.')[0]}_{model_name}_output.mp4"
    )
    if os.path.exists(video_file_name_out):
        os.remove(video_file_name_out)
    result_video_json_file = os.path.join(
        output_folder, f"{video_file.split('.')[0]}_{model_name}_output.json"
    )
    if os.path.exists(result_video_json_file):
        os.remove(result_video_json_file)
    json_file = open(result_video_json_file, "a")
    temp_file = "temp.mp4"
    video_writer = cv2.VideoWriter(
        temp_file,
        cv2.VideoWriter_fourcc(*"mp4v"),
        30,
        (results[0].orig_img.shape[1], results[0].orig_img.shape[0]),
    )
    json_file.write("[\n")
    for result in stqdm(results, desc=f"Processing video"):
        result_list_json = result_to_json(result, tracker=tracker)
        result_image = image_viewer(result, result_list_json, centers=centers)
        video_writer.write(result_image)
        json.dump(result_list_json, json_file, indent=2)
        json_file.write(",\n")
    json_file.write("]")
    video_writer.release()
    subprocess.call(
        args=f"ffmpeg -i {os.path.join('.', temp_file)} -c:v libx264 {os.path.join('.', video_file_name_out)}".split(
            " "
        )
    )
    os.remove(temp_file)
    return video_file_name_out, result_video_json_file


class ObjectDetector(VideoTransformerBase):
    def __init__(self):
        self.tracker = DeepSort(max_age=5)
        self.centers = [deque(maxlen=30) for _ in range(10000)]

    def transform(self, frame):
        image = frame.to_ndarray(format="bgr24")
        image, result_list_json = image_processing(
            image, model, tracker=self.tracker, centers=self.centers
        )
        return image

    def on_stop(self):
        self.tracker.delete_all_tracks()
        self.centers.clear()


st.set_page_config(
    page_title="YOLOv8 Processing App", layout="wide", page_icon="./favicon-yolo.ico"
)
st.title("YOLOv8 Processing App")
# create select box for selecting ultralytics YOLOv8 model
model_select = st.selectbox(
    "Select Ultralytics YOLOv8 model",
    [
        # "yolov8n",
        # "yolov8s",
        # "yolov8m",
        # "yolov8l",
        # "yolov8x",
        # "yolov8n-seg",
        # "yolov8s-seg",
        # "yolov8m-seg",
        # "yolov8l-seg",
        # "yolov8x-seg",
        "best_n"
    ],
)
print(f"Selected ultralytics YOLOv8 model: {model_select}.pt")
model = YOLO(f"{model_select}.pt")  # Model initialization
# model = YOLO("best.pt")  # Model initialization
tab_image, tab_video, tab_live_stream = st.tabs(
    ["Image Processing", "Video Processing", "Live Stream Processing"]
)

with tab_image:
    st.header("Image Processing using YOLOv8")
    image_file = st.file_uploader("Upload an image", type=["jpg", "jpeg", "png"])
    process_image_button = st.button("Process Image")
    if image_file is None and process_image_button:
        st.warning("Please upload an image file to be processed!")
    if image_file is not None and process_image_button:
        img = cv2.imdecode(np.frombuffer(image_file.read(), np.uint8), 1)
        img, result_list_json = image_processing(img, model)
        # print(json.dumps(result_list_json, indent=2))
        st.image(img, caption="Uploaded image", channels="BGR")

with tab_video:
    st.header("Video Processing using YOLOv8")
    video_file = st.file_uploader("Upload a video", type=["mp4"])
    process_video_button = st.button("Process Video")
    if video_file is None and process_video_button:
        st.warning("Please upload a video file to be processed!")
    if video_file is not None and process_video_button:
        tracker = DeepSort(max_age=5)
        centers = [deque(maxlen=30) for _ in range(10000)]
        open(video_file.name, "wb").write(video_file.read())
        video_file_out, result_video_json_file = video_processing(
            video_file.name, model, tracker=tracker, centers=centers
        )
        os.remove(video_file.name)
        video_bytes = open(video_file_out, "rb").read()
        st.video(video_bytes)

with tab_live_stream:
    st.header("Live Stream Processing using YOLOv8")
    webrtc_ctx = webrtc_streamer(
        key="example",
        mode=WebRtcMode.SENDRECV,
        media_stream_constraints={
            "video": True,
            "audio": False,
        },
        video_processor_factory=ObjectDetector,
    )
