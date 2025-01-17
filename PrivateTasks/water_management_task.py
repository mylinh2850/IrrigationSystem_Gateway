import requests
import json
import time
from datetime import datetime, timezone, timedelta
import Ultilities.modbus485 as modbus485
from Ultilities.softwaretimer import softwaretimer
from Adafruit_IO import Client, Feed, RequestError

class WaterManagementTask:
    IDLE = 'IDLE'
    FERTILIZING = 'FERTILIZING'
    MIXING = 'MIXING'
    PUMP_IN = 'PUMP_IN'
    SELECTING_AREA = 'SELECTING_AREA'
    PUMP_OUT = 'PUMP_OUT'

    def __init__(self, modbus, notification_func, aio_username, aio_key, aio_schedule_feed, aio_management_feed):
        self.modbus = modbus
        self.notification_func = notification_func
        self.aio = Client(aio_username, aio_key)
        self.aio_username = aio_username
        self.aio_key = aio_key
        self.aio_schedule_feed = aio_schedule_feed
        self.aio_management_feed = aio_management_feed
        self.state = self.IDLE
        self.current_mixer = 0
        self.mixer_ids = [1, 2, 3]
        self.area_selector_ids = [4, 5, 6]
        self.pump_in_relay_id = 7
        self.pump_out_relay_id = 8
        self.timer = softwaretimer()
        self.schedules = []
        self.current_schedule = None
        self.last_fetched_time = datetime.min.replace(tzinfo=timezone.utc)
        self.last_completed_schedule_name = None

        # Initialize feeds
        self.feeds = {
            'management': self.init_feed(aio_management_feed)
        }

    def init_feed(self, feed_name):
        try:
            feed = self.aio.feeds(feed_name)
        except RequestError:
            feed = self.aio.create_feed(Feed(name=feed_name))
        return feed.key  # Return the feed key

    def update_feed(self, feed_name, value):
        if feed_name in self.feeds:
            json_value = json.dumps(value)
            self.aio.send_data(self.feeds[feed_name], json_value)  # Use feed key to send data

    def activate_relay(self, relay_id, state):
        self.modbus.setDevice(self.modbus.ser, relay_id, state)

    def calculate_total_time(self, schedule):
        # Calculate individual times with an additional 5% buffer
        fertilizer1_time = int(schedule['fertilizer1']) * 0.01
        fertilizer2_time = int(schedule['fertilizer2']) * 0.01
        fertilizer3_time = int(schedule['fertilizer3']) * 0.01
        mixing_time = 10  # Mixing time in seconds
        pump_in_time = int(schedule['waterAmount']) * 0.01
        pump_out_time = pump_in_time
        area_selection_time = 1  # Time to select area in seconds

        total_time = (fertilizer1_time + fertilizer2_time + fertilizer3_time +
                      mixing_time + pump_in_time + pump_out_time + area_selection_time)

        # Add an additional 15% buffer to the total time
        buffer_time = total_time * 0.15
        total_time_with_buffer = total_time + buffer_time

        return total_time_with_buffer

    def check_new_data(self):
        url = f"https://io.adafruit.com/api/v2/{self.aio_username}/feeds/{self.aio_schedule_feed}/data"
        headers = {
            'X-AIO-Key': self.aio_key
        }
        response = requests.get(url, headers=headers)
        if response.status_code == 200:
            feed_data = response.json()
            if feed_data:
                latest_entry = feed_data[0]
                created_at = datetime.fromisoformat(latest_entry['created_at'].replace('Z', '+00:00'))
        
                # Đặt múi giờ của Thành phố Hồ Chí Minh (UTC+7)
                hcm_tz = timezone(timedelta(hours=7))
                
                # Lấy thời gian hiện tại theo múi giờ UTC+7
                current_time = datetime.now(hcm_tz)
        
                # So sánh nếu lịch tưới được tạo trước hiện tại 1 phút
                if created_at > current_time - timedelta(minutes=1):
                    return latest_entry
        return None

    def fetch_schedules(self):
        latest_entry = self.check_new_data()
        if latest_entry:
            schedule = json.loads(latest_entry['value'])
            if schedule['name'] != self.last_completed_schedule_name:
                self.schedules.append(schedule)
                self.last_fetched_time = datetime.fromisoformat(latest_entry['created_at'].replace('Z', '+00:00'))
                print(f"Fetched new schedule: {schedule['name']}")
        else:
            print("No new data on Adafruit IO")

    def run(self):
        if self.state == self.IDLE:
            if not self.schedules:
                self.fetch_schedules()
            if self.schedules:
                self.current_schedule = self.schedules.pop(0)

                # Tính tổng thời gian dự tính hoàn thành
                total_time = self.calculate_total_time(self.current_schedule)

                # Tính thời gian cho từng giai đoạn
                self.fertilizer1_time = int(self.current_schedule['fertilizer1']) * 0.01 * 1000  # convert to milliseconds
                self.fertilizer2_time = int(self.current_schedule['fertilizer2']) * 0.01 * 1000  # convert to milliseconds
                self.fertilizer3_time = int(self.current_schedule['fertilizer3']) * 0.01 * 1000  # convert to milliseconds
                self.mixing_time = 10 * 1000  # Mixing time in milliseconds
                self.pump_in_time = int(self.current_schedule['waterAmount']) * 0.01 * 1000  # convert to milliseconds
                self.pump_out_time = self.pump_in_time
                self.area_selection_time = 1 * 1000  # convert to milliseconds

                # Gửi xác nhận lịch tưới
                confirmation_data = {
                    'schedule_name': self.current_schedule['name'],
                    'status': 'confirmed',
                    'total_time': total_time
                }
                self.update_feed('management', confirmation_data)

                # In ra thông báo
                print(f"Received watering schedule: {self.current_schedule['name']}")
                print(f"Estimated total time to complete schedule: {round(total_time, 2)} seconds")

                self.state = self.FERTILIZING
                self.current_mixer = 0
                self.timer.start(self.fertilizer1_time)
                self.activate_relay(self.mixer_ids[self.current_mixer], True)
                print(f"Started fertilizing with mixer {self.mixer_ids[self.current_mixer]}")

        elif self.state == self.FERTILIZING:
            if self.timer.is_expired():
                self.activate_relay(self.mixer_ids[self.current_mixer], False)
                self.current_mixer += 1
                if self.current_mixer < len(self.mixer_ids):
                    fertilizer_time = getattr(self, f'fertilizer{self.current_mixer + 1}_time')
                    self.timer.start(fertilizer_time)
                    self.activate_relay(self.mixer_ids[self.current_mixer], True)
                    print(f"Started fertilizing with mixer {self.mixer_ids[self.current_mixer]}")
                else:
                    self.state = self.MIXING
                    self.timer.start(self.mixing_time)  # Time in milliseconds
                    print("Started mixing for 10 seconds")

        elif self.state == self.MIXING:
            if self.timer.is_expired():
                self.state = self.PUMP_IN
                self.timer.start(self.pump_in_time)
                self.activate_relay(self.pump_in_relay_id, True)
                print(f"Started pump-in with relay {self.pump_in_relay_id}")

        elif self.state == self.PUMP_IN:
            if self.timer.is_expired():
                self.activate_relay(self.pump_in_relay_id, False)
                self.state = self.SELECTING_AREA
                area = int(self.current_schedule['area']) - 1
                self.timer.start(self.area_selection_time)  # Time in milliseconds
                self.activate_relay(self.area_selector_ids[area], True)
                print(f"Started selecting area {area + 1}")

        elif self.state == self.SELECTING_AREA:
            if self.timer.is_expired():
                area = int(self.current_schedule['area']) - 1
                self.activate_relay(self.area_selector_ids[area], False)
                self.state = self.PUMP_OUT
                self.timer.start(self.pump_out_time)
                self.activate_relay(self.pump_out_relay_id, True)
                print(f"Started pump-out with relay {self.pump_out_relay_id}")

        elif self.state == self.PUMP_OUT:
            if self.timer.is_expired():
                self.activate_relay(self.pump_out_relay_id, False)
                self.state = self.IDLE
                self.notification_func("Cycle complete")
                print("Watering cycle complete")
                self.last_completed_schedule_name = self.current_schedule['name']
                self.current_schedule = None

    def cleanup(self):
        self.modbus.close_modbus()
