#!/usr/bin/env python3

import rospy
import cv2
import numpy as np
from sensor_msgs.msg import Image
from std_msgs.msg import Float32MultiArray, Float64MultiArray, Bool
from cv_bridge import CvBridge, CvBridgeError
import os
import pickle
import time
from keras_segmentation.models.unet import mobilenet_unet

class AutonomousDrivingNode:
    def __init__(self):
        rospy.init_node('AutonomousDrivingNode', anonymous=True)
        self.bridge = CvBridge()
        self.pub1 = rospy.Publisher('Analized_image', Image, queue_size=2)
        self.pub_goal = rospy.Publisher('calculated_goal_state', Float64MultiArray, queue_size=2)

        # Publisher
        rospy.Subscriber("/usb_cam/image_raw", Image, self.image_callback, queue_size=2)
        rospy.Subscriber('ultraDistance', Float64MultiArray, self.ultrasonic_callback)

        self.control_sub = rospy.Subscriber("control_signal", Bool, self.control_callback)
        self.rate = rospy.Rate(15)
        self.width = 448
        self.height = 300
        self.resolution = 17/1024   # 17m/4096 픽셀
        self.laneWidth = 0.90       #원래는 0.85m지만, 조금 조정?

        self.carWidth = 0.42
        self.carBoxHeight = 0.54
        # 차량의 위치 및 크기 정의
        self.car_box_dist = 0.61
        self.car_center_x = int(self.width / 2)
        self.car_center_y = self.height - int(self.car_box_dist / self.resolution/2)
        self.car_TR_center_y = self.height + int((self.car_box_dist+0.3) / self.resolution/2)
        self.car_width = int(self.carWidth /self.resolution)
        self.car_height = int(self.carBoxHeight /self.resolution)
        # 초음파 센서 관련 초기화
        self.num_sensors = 4  # 예제 값, 실제 센서 개수로 변경
        self.sensor_positions = [(0.1, 0.2), (-0.1, 0.2)]  # 예제 값, 실제 센서 위치로 변경
        self.sensor_angles = [0, np.pi / 4, -np.pi / 4, np.pi / 2]  # 예제 값, 실제 센서 각도로 변경
        self.distances = [0] * self.num_sensors

        # 차량 박스 생성
        self.car_box = np.zeros((self.height, self.width), dtype=np.uint8)
        self.top_left_x = max(self.car_center_x - self.car_width // 2, 0)
        self.top_left_y = max(self.car_center_y - self.car_height // 2, 0)
        self.bottom_right_x = min(self.car_center_x + self.car_width // 2, self.width)
        self.bottom_right_y = min(self.car_center_y + self.car_height // 2, self.height)

        self.car_box[self.top_left_y:self.bottom_right_y, self.top_left_x:self.bottom_right_x] = 1
        # Directory and file settings
        self.directory = '/home/innodriver/InnoDriver_ws/src/visionMapping/src/warpMatrix'
        self.warp_matrix = self.load_warp_transform_matrix(self.directory)

        self.trajectory_masks = self.create_trajectory_masks()

        self.max_Angle = 23
        # State variable
        self.running = False
        self.isStart = False
        self.goalLane = 0

        #Unet Model Variable
        self.checkpoints_path = '/home/innodriver/InnoDriver_ws/Unet_train/checkpoints/mobile_unet.58'  # 최신 체크포인트 파일 경로
        self.model = None
        self.model = self.load_model(self.checkpoints_path)

        self.currentAngle = 0
        self.error = False

    def control_callback(self, msg):
        self.running = msg.data
        rospy.loginfo(f"Received control signal: {'Running' if self.running else 'Stopped'}")

    def image_callback(self, msg):
        try:

            cv_image = self.bridge.imgmsg_to_cv2(msg, "bgr8")
            cv_image = self.warp_transform(cv_image,self.width, self.height)
            lane1_mask, lane2_mask = self.create_lane_masks(cv_image)
            
            # Combine masks with different colors
            colored_image = cv_image.copy()
            colored_image[lane1_mask == 1] = [255, 0, 0]  # Blue for 1st lane
            colored_image[lane2_mask == 1] = [0, 0, 255]  # Red for 2nd lane

            lane1CP, lane2CP = self.calculate_carLane_probabilities(lane1_mask, lane2_mask)
            # print(time.time()-startTime)
            # print(lane1P, lane2P)
            # lane1OP, lane2OP, distanceObs = self.calculate_obstacle_probabilities(lane1_mask, lane2_mask)
            
            
            # Convert back to ROS Image message and publish
            transformed_msg = self.bridge.cv2_to_imgmsg(colored_image, "bgr8")
            self.pub1.publish(transformed_msg)

            if not self.isStart and (np.sum(lane2_mask)+np.sum(lane1_mask))>0:
                self.isStart =True
                if lane1CP > lane2CP:
                    self.goalLane = 2
                else:
                    self.goalLane = 2
            current_lane_mask = lane1_mask if self.goalLane==1 else lane2_mask
            self.currentAngle = self.calculate_optimal_steering(current_lane_mask)
            
            pulse_range = 250 - 150
            pulse = 250 - (np.abs(self.currentAngle)/self.max_Angle * pulse_range)
            
            # If not running, set pulse to 0
            if (not self.running) or (self.error):
                pulse = 0

            # Create the goal_state message
            msg = Float64MultiArray()
            msg.data = [0-self.currentAngle/self.max_Angle, pulse / 255]

            # msg.data = [optimal_steering_angle, pulse / 255]
            # msg.data = [predicted_steering, 0]
            self.pub_goal.publish(msg)
            if np.sum(lane2_mask)+np.sum(lane1_mask)==0:
                self.error = True
            else:
                self.error = False

        except CvBridgeError as e:
            self.error = True
            rospy.logerr("Failed to convert image: %s", e)

    def ultrasonic_callback(self, msg):
        self.distances = msg.data

    # 모델 불러오기
    def load_model(self, checkpoints_path):
        model = mobilenet_unet(n_classes=3, input_height=224, input_width=224)
        model.load_weights(checkpoints_path)
        return model

    def create_lane_masks(self, image):        
        # 이미지 크기를 모델 입력 크기에 맞게 조정
        empty_mask = np.zeros((self.height, self.width), dtype=bool)  # 빈 마스크 생성
        if self.model is None:
            rospy.logerr("Model is not loaded")
            return empty_mask, empty_mask
        try:
            frame_resized = cv2.resize(image, (224, 224))
            segmented_image = self.model.predict_segmentation(inp=frame_resized)
            
            # colored_image = image.copy()
            # colored_image[segmented_image == 1] = [255, 0, 0]  # Blue for 1st lane
            # colored_image[segmented_image == 2] = [0, 0, 255]  # Red for 2nd lane
            
            # print(segmented_image.shape)
            # cv2.imshow('segmented_image', colored_image)
            # cv2.waitKey(100)
            if segmented_image is None:
                rospy.logerr("Segmentation failed, returning empty masks")
                return empty_mask, empty_mask
            
            segmented_image_resized = cv2.resize(segmented_image, (self.width, self.height),interpolation=cv2.INTER_NEAREST)

            lane1_mask = segmented_image_resized == 1
            lane2_mask = segmented_image_resized == 2

            return lane1_mask, lane2_mask
        except Exception as e:
            rospy.logerr(f"Error in create_lane_masks: {str(e)}")
            return empty_mask, empty_mask


    def calculate_carLane_probabilities(self, lane1_mask, lane2_mask):

        # 차량 박스와 각 레인 마스크의 겹치는 영역 계산
        intersection_with_lane1 = np.sum(np.logical_and(self.car_box, lane1_mask))
        intersection_with_lane2 = np.sum(np.logical_and(self.car_box, lane2_mask))

        # 차량 박스 내 총 픽셀 수
        total_car_pixels = np.sum(self.car_box)
        # cv2.imshow('carBox Mask', car_box*255)
        # cv2.waitKey(10000)

        # 각 레인에 대한 확률 계산
        lane1_probability = intersection_with_lane1 / total_car_pixels if total_car_pixels > 0 else 0
        lane2_probability = intersection_with_lane2 / total_car_pixels if total_car_pixels > 0 else 0

        return lane1_probability, lane2_probability


    def run(self):
        while not rospy.is_shutdown():
            self.rate.sleep()

    # 기존 warp matrix를 로드하는 함수
    def load_warp_transform_matrix(self, directory, file_name='warp_matrix.pkl'):
        file_path = os.path.join(directory, file_name)
        try:
            with open(file_path, 'rb') as f:
                matrix = pickle.load(f)
            rospy.loginfo("Loaded transform matrix from %s", file_path)
            return matrix
        except FileNotFoundError:
            raise FileNotFoundError(f"Transform matrix file not found at {file_path}. Please generate it first.")

    def warp_transform(self, cv_image,width, height):
        top_view = cv2.warpPerspective(cv_image, self.warp_matrix, (width, height), borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255))
        return top_view

    def calculate_obstacle_probabilities(self, lane1_mask, lane2_mask):
        # 장애물 위치를 계산하고 Gaussian 분포로 Mask 생성
        obstacle_mask = np.zeros((self.height, self.width), dtype=np.uint8)
        sigma = 0.5 / self.resolution  # 1m를 픽셀로 변환한 값

        y, x = np.meshgrid(np.arange(self.height), np.arange(self.width), indexing='ij')

        lane1_probabilities = []
        lane2_probabilities = []
        distances_within_range = []
        for i in range(self.num_sensors):
            distance = self.distances[i]
            if distance > 4000:
                lane1_probabilities.append(0)
                lane2_probabilities.append(0)
                distances_within_range.append(distance)
                continue  # 4m 이상의 장애물은 무시
            
            sensor_x, sensor_y = self.sensor_positions[i]
            angle = self.sensor_angles[i]
            
            # 장애물의 이미지 상 위치 계산
            obstacle_x = sensor_x + distance * np.cos(angle)
            obstacle_y = sensor_y + distance * np.sin(angle)
            obstacle_x = int(obstacle_x / self.resolution)
            obstacle_y = int(obstacle_y / self.resolution)

            # Gaussian 분포로 Mask 생성
            d = np.sqrt((x - obstacle_x) ** 2 + (y - obstacle_y) ** 2)
            gaussian_mask = np.exp(-(d ** 2) / (2 * sigma ** 2))
            obstacle_mask = np.maximum(obstacle_mask, gaussian_mask * 255)
            
            cv2.imshow('Obstacle Mask', obstacle_mask)
            cv2.waitKey(100)
            # Lane mask와 내적하여 확률 계산
            lane1_intersection = np.sum(lane1_mask * obstacle_mask)
            lane2_intersection = np.sum(lane2_mask * obstacle_mask)
            total_intersection = np.sum(obstacle_mask)

            if total_intersection == 0:
                lane1_probability = 0
                lane2_probability = 0
            else:
                lane1_probability = lane1_intersection / total_intersection
                lane2_probability = lane2_intersection / total_intersection

            lane1_probabilities.append(lane1_probability)
            lane2_probabilities.append(lane2_probability)
            distances_within_range.append(distance)

        return lane1_probabilities, lane2_probabilities, distances_within_range

    def create_trajectory_masks(self):
        car_position = (self.car_center_x, self.car_TR_center_y)
        image_size = (self.height, self.width)
        trajectory_masks = [self.create_trajectory_mask(angle, car_position, self.car_width+0.007/self.resolution, self.car_height, image_size) for angle in range(-20, 21)]
        return trajectory_masks
    
    def create_trajectory_mask(self, angle, car_position, car_width, car_height, image_size, decay_factor=0.9):
        # 더 큰 임시 마스크 생성
        larger_size = (image_size[0] * 2, image_size[1] * 2)
        mask = np.zeros(larger_size, dtype=np.float32)
        cx, cy = car_position
        offset_x, offset_y = image_size[1] // 2, image_size[0] // 2
        # print(car_width)
        if angle == 0:
            for t in np.arange(0, 1.9/ self.resolution, 0.5):
                y = int(cy - t) + offset_y
                x1 = int(cx - car_width // 2) + offset_x
                x2 = int(cx + car_width // 2) + offset_x
                if y < 0 or x1 < 0 or x2 >= larger_size[1]:
                    break
                mask[y, x1:x2] = np.exp(-decay_factor * t * self.resolution)
        else:
            radius = np.abs(car_height / np.tan(np.radians(angle)))
            center_x = cx + (radius if angle > 0 else (0-radius)) + offset_x

            for t in np.arange(0, 1.9/ self.resolution, 0.5):
                theta = t / radius
                y = int(cy - radius * np.sin(theta)) + offset_y
                x_center = int(cx - radius * (1 - np.cos(theta))) + offset_x

                if y < 0 or x_center < 0 or x_center >= larger_size[1]:
                    break

                if angle > 0:
                    left_radius = radius - car_width / 2
                    right_radius = radius + car_width / 2
                    x_left = int(center_x - left_radius * np.cos(theta))
                    x_right = int(center_x - right_radius * np.cos(theta))
                    y_left = int(cy - left_radius * np.sin(theta)) + offset_y
                    y_right = int(cy - right_radius * np.sin(theta)) + offset_y
                else:
                    left_radius = radius + car_width / 2
                    right_radius = radius - car_width / 2
                    x_left = int(center_x + left_radius * np.cos(theta))
                    x_right = int(center_x + right_radius * np.cos(theta))
                    y_left = int(cy - left_radius * np.sin(theta)) + offset_y
                    y_right = int(cy - right_radius * np.sin(theta)) + offset_y

                if x_left < 0 or x_right >= larger_size[1] or y_left < 0 or y_right >= larger_size[0]:
                    break

                # Draw lines between (x_left, y_left) and (x_right, y_right)
                cv2.line(mask, (x_left, y_left), (x_right, y_right), np.exp(-decay_factor * t * self.resolution), thickness=1)

        # 원래 크기로 자르기
        mask = mask[offset_y:image_size[0] + offset_y, offset_x:image_size[1] + offset_x]
        mask = (mask * 255).astype(np.uint8)
        return mask
    
    def calculate_optimal_steering(self, lane_mask):
        max_dot_product = -1
        optimal_angle = 0

        for angle, mask in zip(range(-20, 21), self.trajectory_masks):
            
            # cv2.imshow('lane_mask*trajectory Mask', lane_mask * mask)
            # cv2.waitKey(100)
            dot_product = np.sum(lane_mask * mask)
            if dot_product > max_dot_product:
                max_dot_product = dot_product
                optimal_angle = angle
        
        # cv2.imshow('lane_mask*trajectory Mask', self.trajectory_masks[angle+20] * lane_mask)
        # cv2.waitKey(1)
        return optimal_angle

if __name__ == '__main__':
    try:
        lane_masker = AutonomousDrivingNode()
        lane_masker.run()

        # # 이미지 파일 경로
        # image_path = "/home/innodriver/InnoDriver_ws/src/missionRacing/src/1720785185578291177.jpg"

        # # 이미지 읽기
        # image = cv2.imread(image_path)
        # startTime = time.time()
        # transformedImage = lane_masker.warp_transform(image,lane_masker.width, lane_masker.height)
        # # print(time.time()-startTime)
        # lane1_mask, lane2_mask = lane_masker.create_lane_masks(transformedImage)
        
        # # Combine masks with different colors
        # colored_image = transformedImage.copy()
        # colored_image[lane1_mask == 1] = [128, 0, 0]  # Blue for 1st lane
        # colored_image[lane2_mask == 1] = [0, 0, 128]  # Red for 2nd lane
        # cv2.imshow('Road Mask', colored_image)
        # lPoint, rPoint = lane_masker.calculate_waypoints(corners)
        # colored_image = lane_masker.draw_waypoints_on_mask(colored_image, lPoint, rPoint)
        # lane1P, lane2P = lane_masker.calculate_carLane_probabilities(lane1_mask, lane2_mask)
        # print(time.time()-startTime)
        # print(lane1P, lane2P)

        # for i in range(len(lane_masker.trajectory_masks)):
        #     cv2.imshow('trajectory_masks', lane_masker.trajectory_masks[i])
        #     cv2.waitKey(500)


        # current_lane_mask = lane1_mask if lane1P > lane2P else lane2_mask
        # optimal_steering_angle = lane_masker.calculate_optimal_steering(current_lane_mask)
        # print(f"Optimal Steering Angle: {optimal_steering_angle} degrees")
        # print(time.time()-startTime)
        # cv2.imshow('waypoint Mask', colored_image)
        # cv2.waitKey(10000)
    except rospy.ROSInterruptException:
        pass
