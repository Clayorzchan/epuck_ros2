# Copyright 1996-2020 Cyberbotics Ltd.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import rclpy
from math import pi, cos, sin
from .controller import Robot
from nav_msgs.msg import Odometry
from sensor_msgs.msg import Range, LaserScan
from tf2_ros import TransformBroadcaster
from geometry_msgs.msg import Twist, Quaternion, TransformStamped
from .webots_node import WebotsNode
import time
from builtin_interfaces.msg import Time
from rcl_interfaces.msg import SetParametersResult


def euler_to_quaternion(roll, pitch, yaw):
    """Source: https://computergraphics.stackexchange.com/a/8229."""
    q = Quaternion()
    q.x = sin(roll/2) * cos(pitch/2) * cos(yaw/2) - \
        cos(roll/2) * sin(pitch/2) * sin(yaw/2)
    q.y = cos(roll/2) * sin(pitch/2) * cos(yaw/2) + \
        sin(roll/2) * cos(pitch/2) * sin(yaw/2)
    q.z = cos(roll/2) * cos(pitch/2) * sin(yaw/2) - \
        sin(roll/2) * sin(pitch/2) * cos(yaw/2)
    q.w = cos(roll/2) * cos(pitch/2) * cos(yaw/2) + \
        sin(roll/2) * sin(pitch/2) * sin(yaw/2)
    return q


def intensity_to_distance(p_x):
    table = [
        [0, 4095],
        [0.005, 2133.33],
        [0.01, 1465.73],
        [0.015, 601.46],
        [0.02, 383.84],
        [0.03, 234.93],
        [0.04, 158.03]
    ]
    for i in range(len(table) - 1):
        if table[i][1] >= p_x and table[i+1][1] < p_x:
            b_x = table[i][1]
            b_y = table[i][0]
            a_x = table[i+1][1]
            a_y = table[i+1][0]
            p_y = ((b_y - a_y) / (b_x - a_x)) * (p_x - a_x) + a_y
            return p_y
    return 0.0


class EPuck2Controller(WebotsNode):
    def __init__(self, name, args=None):
        super().__init__(name)
        self.robot = Robot()

        # Parameters
        wheel_distance_param = self.declare_parameter("wheel_distance", 0.0552)
        wheel_radius_param = self.declare_parameter("wheel_radius", 0.021)
        self.timestep = self.declare_parameter("timestep", 64)
        self.wheel_radius = wheel_radius_param.value
        self.wheel_distance = wheel_distance_param.value
        self.set_parameters_callback(self.on_param_changed)

        # Init motors
        self.left_motor = self.robot.getMotor('left wheel motor')
        self.right_motor = self.robot.getMotor('right wheel motor')
        self.left_motor.setPosition(float('inf'))
        self.right_motor.setPosition(float('inf'))
        self.left_motor.setVelocity(0)
        self.right_motor.setVelocity(0)
        self.create_subscription(Twist, '/cmd_vel', self.cmd_vel_callback, 1)
        self.get_logger().info('EPuck Initialized')

        # Initialize odometry
        self.reset_odometry()
        self.left_wheel_sensor = self.robot.getPositionSensor(
            'left wheel sensor')
        self.right_wheel_sensor = self.robot.getPositionSensor(
            'right wheel sensor')
        self.left_wheel_sensor.enable(self.timestep.value)
        self.right_wheel_sensor.enable(self.timestep.value)
        self.odometry_publisher = self.create_publisher(Odometry, '/odom', 1)

        # Intialize distance sensors
        self.sensor_publishers = {}
        self.sensors = {}
        for i in range(8):
            sensor = self.robot.getDistanceSensor('ps{}'.format(i))
            sensor.enable(self.timestep.value)
            sensor_publisher = self.create_publisher(
                Range, '/distance/ps{}'.format(i), 10)
            self.sensors['ps{}'.format(i)] = sensor
            self.sensor_publishers['ps{}'.format(i)] = sensor_publisher

        sensor = self.robot.getDistanceSensor('tof')
        sensor.enable(self.timestep.value)
        sensor_publisher = self.create_publisher(Range, '/distance/tof', 1)
        self.sensors['tof'] = sensor
        self.sensor_publishers['tof'] = sensor_publisher

        self.laser_publisher = self.create_publisher(LaserScan, '/scan', 1)

        # Steps...
        self.create_timer(self.timestep.value / 1000, self.step_callback)

        # Transforms
        self.tf_broadcaster = TransformBroadcaster(self)

        self.tf_laser_scanner = TransformStamped()
        self.tf_laser_scanner.header.frame_id = 'base_footprint'
        self.tf_laser_scanner.child_frame_id = 'laser_scanner'
        self.tf_laser_scanner.transform.translation.x = 0.0
        self.tf_laser_scanner.transform.translation.y = 0.0
        self.tf_laser_scanner.transform.translation.z = 0.0
        self.tf_laser_scanner.transform.rotation = euler_to_quaternion(0, 0, 0)

    def reset_odometry(self):
        self.prev_left_wheel_ticks = 0
        self.prev_right_wheel_ticks = 0
        self.prev_position = (0.0, 0.0)
        self.prev_angle = 0.0

    def on_param_changed(self, params):
        result = SetParametersResult()
        result.successful = True

        for param in params:
            if param.name == "wheel_radius":
                self.reset_odometry()
                self.wheel_radius = param.value
            elif param.name == "wheel_distance":
                self.reset_odometry()
                self.wheel_distance = param.value

        return result

    def step_callback(self):
        self.robot.step(self.timestep.value)

        epoch = time.time()
        stamp = Time()
        stamp.sec = int(epoch)
        stamp.nanosec = int((epoch - int(epoch)) * 1E9)

        self.odometry_callback(stamp)
        self.distance_callback(stamp)
        self.publish_static_transforms(stamp)

    def publish_static_transforms(self, stamp):
        # Pack & publish transforms
        self.tf_laser_scanner.header.stamp = stamp
        self.tf_broadcaster.sendTransform(self.tf_laser_scanner)

    def cmd_vel_callback(self, twist):
        self.get_logger().info('Twist message received')
        left_velocity = (2.0 * twist.linear.x - twist.angular.z *
                         self.wheel_distance) / (2.0 * self.wheel_radius)
        right_velocity = (2.0 * twist.linear.x + twist.angular.z *
                          self.wheel_distance) / (2.0 * self.wheel_radius)
        self.left_motor.setVelocity(left_velocity)
        self.right_motor.setVelocity(right_velocity)

    def odometry_callback(self, stamp):
        encoder_period_s = self.timestep.value / 1000.0
        left_wheel_ticks = self.left_wheel_sensor.getValue()
        right_wheel_ticks = self.right_wheel_sensor.getValue()

        # Calculate velocities
        v_left_rad = (left_wheel_ticks -
                      self.prev_left_wheel_ticks) / encoder_period_s
        v_right_rad = (right_wheel_ticks -
                       self.prev_right_wheel_ticks) / encoder_period_s
        v_left = v_left_rad * self.wheel_radius
        v_right = v_right_rad * self.wheel_radius
        v = (v_left + v_right) / 2
        omega = (v_right - v_left) / self.wheel_distance

        # Calculate position & angle
        # Fourth order Runge - Kutta
        # Reference: https://www.cs.cmu.edu/~16311/s07/labs/NXTLabs/Lab%203.html
        k00 = v * cos(self.prev_angle)
        k01 = v * sin(self.prev_angle)
        k02 = omega
        k10 = v * cos(self.prev_angle + encoder_period_s * k02 / 2)
        k11 = v * sin(self.prev_angle + encoder_period_s * k02 / 2)
        k12 = omega
        k20 = v * cos(self.prev_angle + encoder_period_s * k12 / 2)
        k21 = v * sin(self.prev_angle + encoder_period_s * k12 / 2)
        k22 = omega
        k30 = v * cos(self.prev_angle + encoder_period_s * k22 / 2)
        k31 = v * sin(self.prev_angle + encoder_period_s * k22 / 2)
        k32 = omega
        position = [
            self.prev_position[0] + (encoder_period_s / 6) *
            (k00 + 2 * (k10 + k20) + k30),
            self.prev_position[1] + (encoder_period_s / 6) *
            (k01 + 2 * (k11 + k21) + k31)
        ]
        angle = self.prev_angle + \
            (encoder_period_s / 6) * (k02 + 2 * (k12 + k22) + k32)

        # Update variables
        self.prev_position = position.copy()
        self.prev_angle = angle
        self.prev_left_wheel_ticks = left_wheel_ticks
        self.prev_right_wheel_ticks = right_wheel_ticks

        # Pack & publish odometry
        msg = Odometry()
        msg.header.stamp = stamp
        msg.header.frame_id = 'odom'
        msg.child_frame_id = 'base_footprint'
        msg.twist.twist.linear.x = v
        msg.twist.twist.linear.z = omega
        msg.pose.pose.position.x = position[0]
        msg.pose.pose.position.y = position[1]
        msg.pose.pose.orientation = euler_to_quaternion(0, 0, angle)
        self.odometry_publisher.publish(msg)

        # Pack & publish transforms
        tf = TransformStamped()
        tf.header.stamp = stamp
        tf.header.frame_id = 'odom'
        tf.child_frame_id = 'base_footprint'
        tf.transform.translation.x = position[0]
        tf.transform.translation.y = position[1]
        tf.transform.translation.z = 0.0
        tf.transform.rotation = euler_to_quaternion(0, 0, angle)
        self.tf_broadcaster.sendTransform(tf)

    def distance_callback(self, stamp):
        distance_from_center = 0.035

        for key in self.sensors:
            msg = Range()
            msg.field_of_view = self.sensors[key].getAperture()
            msg.min_range = intensity_to_distance(
                self.sensors[key].getMaxValue() - 8.2) + distance_from_center
            msg.max_range = intensity_to_distance(
                self.sensors[key].getMinValue() + 3.3) + distance_from_center
            msg.range = intensity_to_distance(self.sensors[key].getValue())
            msg.radiation_type = Range.INFRARED
            self.sensor_publishers[key].publish(msg)

        # Max range of ToF sensor is 2m so we put it as maximum laser range.
        # Therefore, for all invalid ranges we put 0 so it get deleted by rviz

        msg = LaserScan()
        msg.header.frame_id = 'laser_scanner'
        msg.header.stamp = stamp
        msg.angle_min = 0.0
        msg.angle_max = 2 * pi
        msg.angle_increment = 15 * pi / 180.0
        msg.scan_time = self.timestep.value / 1000
        msg.range_min = intensity_to_distance(
            self.sensors['ps0'].getMaxValue() - 20) + distance_from_center
        msg.range_max = 1.0 + distance_from_center
        msg.ranges = [
            self.sensors['tof'].getValue(),                         # 0
            intensity_to_distance(self.sensors['ps7'].getValue()),  # 15
            0.0,                                                    # 30
            intensity_to_distance(self.sensors['ps6'].getValue()),  # 45
            0.0,                                                    # 60
            0.0,                                                    # 75
            intensity_to_distance(self.sensors['ps5'].getValue()),  # 90
            0.0,                                                    # 105
            0.0,                                                    # 120
            0.0,                                                    # 135
            intensity_to_distance(self.sensors['ps4'].getValue()),  # 150
            0.0,                                                    # 165
            0.0,                                                    # 180
            0.0,                                                    # 195
            intensity_to_distance(self.sensors['ps3'].getValue()),  # 210
            0.0,                                                    # 225
            0.0,                                                    # 240
            0.0,                                                    # 255
            intensity_to_distance(self.sensors['ps2'].getValue()),  # 270
            0.0,                                                    # 285
            0.0,                                                    # 300
            intensity_to_distance(self.sensors['ps1'].getValue()),  # 315
            0.0,                                                    # 330
            intensity_to_distance(self.sensors['ps0'].getValue()),  # 345
            self.sensors['tof'].getValue(),                         # 0
        ]
        for i in range(len(msg.ranges)):
            if msg.ranges[i] != 0:
                msg.ranges[i] += distance_from_center

        self.laser_publisher.publish(msg)


def main(args=None):
    rclpy.init(args=args)

    epuck2_controller = EPuck2Controller('epuck2', args=args)

    rclpy.spin(epuck2_controller)

    epuck2_controller.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()
