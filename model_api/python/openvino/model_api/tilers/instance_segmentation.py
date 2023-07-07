"""
 Copyright (c) 2023 Intel Corporation

 Licensed under the Apache License, Version 2.0 (the "License");
 you may not use this file except in compliance with the License.
 You may obtain a copy of the License at

      http://www.apache.org/licenses/LICENSE-2.0

 Unless required by applicable law or agreed to in writing, software
 distributed under the License is distributed on an "AS IS" BASIS,
 WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 See the License for the specific language governing permissions and
 limitations under the License.
"""

import cv2 as cv
import numpy as np
from openvino.model_api.models.instance_segmentation import _segm_postprocess
from openvino.model_api.models.utils import InstanceSegmentationResult, SegmentedObject

from .detection import DetectionTiler, _multiclass_nms


class InstanceSegmentationTiler(DetectionTiler):
    """
    Tiler for object instance segmentation models.
    This tiler expects model to output a lsit of `SegmentedObject` objects.

    In addition, this tiler allows to use a tile classifier model,
    which predicts objectness score for each tile. Later, tiles can
    be filtered by this score.
    """

    def __init__(
        self,
        model,
        configuration=None,
        execution_mode="async",
        tile_classifier_model=None,
    ):
        """
        Constructor for creating a semantic segmentation tiling pipeline

        Args:
            model: underlying model
            configuration: it contains values for parameters accepted by specific
            tiler (`tile_size`, `tiles_overlap` etc.) which are set as data attributes.
            execution_mode: Controls inference mode of the tiler (`async` or `sync`).
            tile_classifier_model: an `ImageModel`, which has "tile_prob" output.
        """
        super().__init__(model, configuration, execution_mode)
        self.tile_classifier_model = tile_classifier_model

    def _filter_tiles(self, image, tile_coords, confidence_threshold=0.35):
        """Filter tiles by objectness score provided by tile classifier

        Args:
             image: full size image
             tile_coords: tile coordinates

        Returns:
             tile coordinates to keep
        """
        if self.tile_classifier_model is not None:
            keep_coords = []
            for i, coord in enumerate(tile_coords):
                tile_img = self._crop_tile(image, coord)
                tile_dict, _ = self.model.preprocess(tile_img)
                cls_outputs = self.tile_classifier_model.infer_sync(tile_dict)
                if i == 0 or cls_outputs["tile_prob"] > confidence_threshold:
                    keep_coords.append(coord)
            return keep_coords

        return tile_coords

    def _postprocess_tile(self, predictions, coord):
        """Converts predictions to a format convinient for further merging.

        Args:
             predictions: predictions from an instance segmentation model: a list of `SegmentedObject` objects
             coord: a list containing coordinates for the processed tile

        Returns:
             a dict with postprocessed detections in 6-items format: (label id, score, bbox) and masks
        """

        output_dict = super()._postprocess_tile(predictions, coord)
        output_dict["masks"] = []
        for segm_res in predictions.segmentedObjects:
            output_dict["masks"].append(segm_res.mask)

        return output_dict

    def _merge_results(self, results, shape):
        """Merge results from all tiles.

        To merge detections, per-class NMS is applied.

        Args:
             results: list of per-tile results
             shape: original full-res image shape
        Returns:
             merged prediciton
        """

        detections_array = np.empty((0, 6), dtype=np.float32)
        feature_vectors = []
        saliency_maps = []
        tiles_coords = []
        masks = []
        for result in results:
            if len(result["bboxes"]):
                detections_array = np.concatenate((detections_array, result["bboxes"]))
            feature_vectors.append(result["features"])
            saliency_maps.append(result["saliency_map"])
            tiles_coords.append(result["coords"])
            if len(result["masks"]):
                masks.extend(result["masks"])

        keep_idxs = []
        if np.prod(detections_array.shape):
            detections_array, keep_idxs = _multiclass_nms(
                detections_array, max_num=self.max_pred_number
            )
        masks = [masks[keep_idx] for keep_idx in keep_idxs]

        merged_vector = (
            np.mean(feature_vectors, axis=0) if feature_vectors else np.ndarray(0)
        )
        saliency_map = (
            self._merge_saliency_maps(saliency_maps, shape, tiles_coords)
            if saliency_maps
            else []
        )

        detected_objects = []
        for i in range(detections_array.shape[0]):
            label = int(detections_array[i][0])
            score = float(detections_array[i][1])
            bbox = list(detections_array[i][2:])
            detected_objects.append(
                SegmentedObject(*bbox, score, label, self.model.labels[label], masks[i])
            )

        for i, (det, mask) in enumerate(zip(detected_objects, masks)):
            box = np.array([det.xmin, det.ymin, det.xmax, det.ymax])
            masks[i] = _segm_postprocess(box, mask, *shape[:-1])

        return InstanceSegmentationResult(
            detected_objects,
            saliency_map,
            merged_vector,
        )

    def _merge_saliency_maps(self, saliency_maps, shape, tiles_coords):
        """Merged saliency maps from each tile

        Args:
            saliency_maps: list of saliency maps, shape of each map is (Nc, H, W)
            shape: shape of the original image
            tiles_coords: coordinates of tiles

        Returns:
            Merged saliency map with shape (Nc, H, W)
        """

        if not saliency_maps:
            return None

        image_saliency_map = saliency_maps[0]

        if not image_saliency_map:
            return image_saliency_map

        num_classes = len(image_saliency_map)
        map_h, map_w = image_saliency_map[0].shape
        image_h, image_w, _ = shape

        ratio = map_h / self.tile_size, map_w / self.tile_size
        image_map_h = int(image_h * ratio[0])
        image_map_w = int(image_w * ratio[1])

        merged_map = [np.zeros((image_map_h, image_map_w)) for _ in range(num_classes)]

        for i, saliency_map in enumerate(saliency_maps[1:], 1):
            for class_idx in range(num_classes):
                cls_map = saliency_map[class_idx]
                if len(cls_map.shape) < 2:
                    continue

                x_1, y_1, x_2, y_2 = tiles_coords[i]
                y_1, x_1 = int(y_1 * ratio[0]), int(x_1 * ratio[1])
                y_2, x_2 = int(y_2 * ratio[0]), int(x_2 * ratio[1])

                map_h, map_w = cls_map.shape

                cls_map = cv.resize(cls_map, (x_2 - x_1, y_2 - y_1))

                map_h, map_w = y_2 - y_1, x_2 - x_1

                tile_map = merged_map[class_idx][y_1 : y_1 + map_h, x_1 : x_1 + map_w]
                merged_map[class_idx][
                    y_1 : y_1 + map_h, x_1 : x_1 + map_w
                ] = np.maximum(tile_map, cls_map)

        for class_idx in range(num_classes):
            image_map_cls = image_saliency_map[class_idx]
            if len(image_map_cls.shape) < 2:
                continue
            image_map_cls = cv.resize(image_map_cls, (image_map_w, image_map_h))
            merged_map[class_idx] += 0.5 * image_map_cls
            merged_map[class_idx] = merged_map[class_idx].astype(np.uint8)

        return merged_map