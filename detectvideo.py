import ffmpeg
import json
import os
import subprocess as sp
from tensorflow.compat.v1 import InteractiveSession
from tensorflow.compat.v1 import ConfigProto
import numpy as np
import cv2
from PIL import Image
from tensorflow.python.saved_model import tag_constants
from core.yolov4 import filter_boxes
import core.utils as utils
from absl.flags import FLAGS
from absl import app, flags, logging
import time
import tensorflow as tf
physical_devices = tf.config.experimental.list_physical_devices('GPU')
if len(physical_devices) > 0:
    tf.config.experimental.set_memory_growth(physical_devices[0], True)

flags.DEFINE_string('framework', 'tf', '(tf, tflite, trt')
flags.DEFINE_string('weights', './checkpoints/yolov4-416',
                    'path to weights file')
flags.DEFINE_integer('size', 416, 'resize images to')
flags.DEFINE_boolean('tiny', False, 'yolo or yolo-tiny')
flags.DEFINE_string('model', 'yolov4', 'yolov3 or yolov4')
flags.DEFINE_string('video', './data/road.mp4', 'path to input video')
flags.DEFINE_float('iou', 0.3, 'iou threshold')
flags.DEFINE_float('score', 0.25, 'score threshold')
flags.DEFINE_string('output', None, 'path to output video')
flags.DEFINE_string('bbox_path', None, 'path to bbox json')
flags.DEFINE_boolean('verbose', False, 'Log current frame id')
flags.DEFINE_string('output_format', 'XVID',
                    'codec used in VideoWriter when saving video to file')
# this is good for the .ipynb
flags.DEFINE_boolean('dis_cv2_window', False,
                     'disable cv2 window during the process')


def export_bbox(image, bboxes):
    out_boxes, out_scores, out_classes, num_boxes = bboxes
    annotations = []
    image_h, image_w, _ = image.shape
    for i in range(num_boxes[0]):
        result = {}
        if int(out_classes[0][i]) < 0 or int(out_classes[0][i]) > 20:
            continue
        coor = out_boxes[0][i]
        coor[0] = int(coor[0])
        coor[2] = int(coor[2])
        coor[1] = int(coor[1])
        coor[3] = int(coor[3])

        score = out_scores[0][i]
        class_ind = int(out_classes[0][i])
        result['bbox'] = coor.tolist()
        result['score'] = score.astype(float)
        result['label'] = class_ind
        annotations.append(result)
    return annotations


def convert_video_numpy(file_name):
    out, _ = (
        ffmpeg
        .input('in.mp4')
        .output('pipe:', format='rawvideo', pix_fmt='rgb24')
        .run(capture_stdout=True)
    )
    video = (
        np
        .frombuffer(out, np.uint8)
    )
    return video


def main(_argv):
    config = ConfigProto()
    config.gpu_options.allow_growth = True
    session = InteractiveSession(config=config)
    STRIDES, ANCHORS, NUM_CLASS, XYSCALE = utils.load_config(FLAGS)
    input_size = FLAGS.size
    video_path = FLAGS.video
    output_vid = FLAGS.output
    bbox_path = FLAGS.bbox_path

    print("Video from: ", video_path)
    for ind in range(1, 26):
        record = {}
        record['videoID'] = ind
        print("Start running video {}".format(ind))

        video_id = 'cam_' + str(ind).zfill(2)
        output_bbox = os.path.join(bbox_path, video_id + '.json')
        output_fname = os.path.join(output_vid, video_id + '.mp4')
        video_fname = os.path.join(video_path, video_id + '.mp4')

        print("Video path:" + video_fname)
        print("Output path:" + output_fname)
        print("Bbox path:" + output_bbox)
        frames = convert_video_numpy(video_fname)
        n, height, width, channels = frames.shape
        process = (
            ffmpeg
            .input('pipe:', format='rawvideo', pix_fmt='rgb', s='{}x{}'.format(width, height))
            .output(output_fname, pix_fmt='yuv420p', vcodec='libx264', r=60)
            .overwrite_output()
            .run_async(pipe_stdin=True)
        )   
        if FLAGS.framework == 'tflite':
            interpreter = tf.lite.Interpreter(model_path=FLAGS.weights)
            interpreter.allocate_tensors()
            input_details = interpreter.get_input_details()
            output_details = interpreter.get_output_details()
            print(input_details)
            print(output_details)
        else:
            saved_model_loaded = tf.saved_model.load(
                FLAGS.weights, tags=[tag_constants.SERVING])
            infer = saved_model_loaded.signatures['serving_default']

        frame_id = 0
        with open(output_bbox, "w") as f:
            f.write('[\n')
        for frame in frames:
            record['frameID'] = frame_id
            frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(frame)

            frame_size = frame.shape[:2]
            image_data = cv2.resize(frame, (input_size, input_size))
            image_data = image_data / 255.
            image_data = image_data[np.newaxis, ...].astype(np.float32)

            if FLAGS.framework == 'tflite':
                interpreter.set_tensor(input_details[0]['index'], image_data)
                interpreter.invoke()
                pred = [interpreter.get_tensor(
                    output_details[i]['index']) for i in range(len(output_details))]
                if FLAGS.model == 'yolov3' and FLAGS.tiny == True:
                    boxes, pred_conf = filter_boxes(pred[1], pred[0], score_threshold=0.25,
                                                    input_shape=tf.constant([input_size, input_size]))
                else:
                    boxes, pred_conf = filter_boxes(pred[0], pred[1], score_threshold=0.25,
                                                    input_shape=tf.constant([input_size, input_size]))
            else:
                batch_data = tf.constant(image_data)
                pred_bbox = infer(batch_data)
                for key, value in pred_bbox.items():
                    boxes = value[:, :, 0:4]
                    pred_conf = value[:, :, 4:]

            boxes, scores, classes, valid_detections = tf.image.combined_non_max_suppression(
                boxes=tf.reshape(boxes, (tf.shape(boxes)[0], -1, 1, 4)),
                scores=tf.reshape(
                    pred_conf, (tf.shape(pred_conf)[0], -1, tf.shape(pred_conf)[-1])),
                max_output_size_per_class=50,
                max_total_size=50,
                iou_threshold=FLAGS.iou,
                score_threshold=FLAGS.score
            )
            pred_bbox = [boxes.numpy(), scores.numpy(
            ), classes.numpy(), valid_detections.numpy()]
            image = utils.draw_bbox(frame, pred_bbox)
            record['annotations'] = export_bbox(frame, pred_bbox)
            with open(output_bbox, 'a') as f:
                if (frame_id > 0):
                    f.write(",\n")
                json.dump(record, f, indent=4)

            result = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
            if not FLAGS.dis_cv2_window:
                cv2.namedWindow("result", cv2.WINDOW_AUTOSIZE)
                cv2.imshow("result", result)
                if cv2.waitKey(1) & 0xFF == ord('q'):
                    break

            if FLAGS.output:
                process.stdin.write(
                frame
                    .astype(np.uint8)
                    .tobytes()
                )
            if (FLAGS.verbose):
                print("FrameID: {}".format(frame_id))
                print("Video ID: {}".format(ind))
                print("Annotations count: {}".format(
                    len(record['annotations'])))
            frame_id += 1
        curr_time = time.time()
        exec_time = curr_time - prev_time
        info = "time: %.2f ms" % (1000*exec_time)
        # print(info)
        process.stdin.close()
        process.wait()
        with open(output_bbox, "a") as f:
            f.write(']')


if __name__ == '__main__':
    try:
        app.run(main)
    except SystemExit:
        pass
