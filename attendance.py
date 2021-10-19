import os
import locale

os.environ["KIVY_WINDOW"] = "sdl2"
os.environ["KIVY_GL_BACKEND"] = "gl"
import logging
import json
import pprint
import graypy

from furl import furl
from time import sleep
from multiprocessing import Process, Queue
from subprocess import run, TimeoutExpired, CalledProcessError
from datetime import datetime, timedelta
from itertools import cycle
from netifaces import interfaces, ifaddresses, gateways, AF_INET, AF_INET6, AF_LINK
from base64 import b64encode
from platform import platform, python_version

from pyrc522 import RFID
from RPi import GPIO

import kivy

logger = logging.getLogger(__name__)
ch = logging.FileHandler("attendance.warn")
ch.setLevel(logging.DEBUG)
logger.addHandler(ch)

kivy.require("2.0.0")

from kivy.app import App
from kivy.clock import Clock
from kivy.config import Config
from kivy.graphics.svg import Svg
from kivy.network.urlrequest import UrlRequest
from kivy.properties import (
    StringProperty,
    ObjectProperty,
    NumericProperty,
    ListProperty,
)
from kivy.uix.scatter import Scatter
from kivy.uix.widget import Widget
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.settings import Settings
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.modalview import ModalView
from kivy.animation import Animation


__version__ = (0, 0, 3)


class ContextFilter(logging.Filter):

    def __init__(self, terminal, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.terminal = terminal


    def filter(self, record):
        record.terminal = self.terminal
        return True


class ErrorScreen(Screen):
    message = ObjectProperty(None)


class QuestionLabel(Label):
    pass


class AnswerButton(Button):
    data = ObjectProperty(None)


class CardReader:

    cache = None
    running = True
    last = datetime.now()
    delta = timedelta(seconds=10)

    def stop(self):
        self.running = False

    def __call__(self, queue):
        rfid = RFID()
        logger.info("Starting card reader")
        while self.running:
            sleep(0.2)
            logger.debug("Reading next card")
            rfid.wait_for_tag()
            (error, tag_type) = rfid.request()
            if error:
                logger.debug("Error at RFID request")
                continue
            (error, uid) = rfid.anticoll()
            if error:
                logger.debug("Error at RFID anticoll")
                continue
            if not uid:
                logger.debug("No UID present")
                continue
            if self.cache == uid:
                logger.debug(f"UID {uid} in cache")
                if datetime.now() - self.last < self.delta:
                    logger.debug(f"UID cache not expired yet")
                    continue
            logger.info(f"Got UID: {uid}")
            data = None
            if not rfid.select_tag(uid):
                if not rfid.card_auth(rfid.auth_a, 4, [0xFF, 0xFF, 0xFF, 0xFF, 0xFF, 0xFF], uid):
                    read, data = rfid.read(4)
                    if read:
                        logger.debug("Could not read admin sector 4")
            rfid.stop_crypto()
            self.cache = uid
            self.last = datetime.now()
            queue.put((tuple(uid), data))


class Loading(FloatLayout):
    angle = NumericProperty(0)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        anim = Animation(angle=360, duration=4)
        anim += Animation(angle=360, duration=4)
        anim.repeat = True
        anim.start(self)

    def on_angle(self, item, angle):
        if angle == 360:
            item.angle = 0


class Flipper(FloatLayout):
    scale = NumericProperty(1)
    cycle = ListProperty([])

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        anim = Animation(scale=1.2, duration=3, t="in_back")
        anim += Animation(scale=1, duration=3, t="out_back")
        anim.repeat = True
        anim.start(self)

    def on_cycle(self, item, values):
        self.iterator = cycle(values)
        self.image.source = next(self.iterator)

    def on_scale(self, item, scale):
        if scale == 1:
            item.image.source = next(item.iterator)


class Attendance(FloatLayout):
    # def on_touch_down(self, touch):
    #    logger.info(f"attednance widget {touch}")
    #    return super().on_touch_down(touch)
    manager = ObjectProperty(None)


class AttendanceApp(App):

    title = "Attendance Terminal"
    time = StringProperty(None)
    active = datetime.now()
    admins = [(116, 223, 167, 235, 231)]
    resetable_timers = []
    network = {}
    token = None
    request_icons = cycle(("images/arrow-circle-right.png", "images/id-card.png"))
    answers = {}

    def __init__(self, queue):
        super().__init__()
        self.queue = queue

    def get_application_config(self):
        return super().get_application_config("~/.%(appname)s.ini")

    def build_config(self, config):
        config.setdefaults("kivy", {"log_enable": "1", "log_level": "warn"})
        config.setdefaults("graphics", {"fullscreen": "auto", "show_cursor": "0"})
        config.setdefaults(
            "api",
            {
                "base_url": "https://api.medunigraz.at/",
                "username": "terminal",
                "password": "",
                "terminal": "1",
                "screenshots": "1",
            }
        )
        config.setdefaults(
            "terminal",
            {
                "sounddetection": "12",
                "screensaver": "60",
                "locale": "de_AT.UTF-8",
                "admin_keys": "12345678",
                "brightness": "50",
            }
        )
        config.setdefaults(
            "graylog",
            {
                "hostname": "graylog.medunigraz.at",
                "port": "12201",
                "level": "DEBUG",
            }
        )

    def build(self):
        Clock.schedule_interval(self.check_card, 0.5)
        Clock.schedule_interval(self.update_time, 1)
        Clock.schedule_interval(self.update_network, 60)
        Clock.schedule_interval(self.screensaver, 5)
        Clock.schedule_interval(self.fetch_token, 60)
        Clock.schedule_interval(self.upload_config, 30)
        Clock.schedule_interval(self.upload_screenshot, 30)
        # Clock.schedule_interval(self.flip_request, 2)
        self.update_time()
        self.update_network()
        self.fetch_token()
        self.root = Attendance()
        self.settings = Settings()
        self.settings.add_kivy_panel()
        self.settings.bind(on_close=self.reset)
        self.settings.add_json_panel("API", self.config, "settings/api.json")
        self.settings.add_json_panel("Terminal", self.config, "settings/terminal.json")
        self.settings.add_json_panel("Graylog", self.config, "settings/graylog.json")
        self.root.manager.get_screen("Settings").add_widget(self.settings)
        locale.setlocale(locale.LC_ALL, self.config.get("terminal", "locale"))
        self.brightness("terminal", "brightness", self.config.get("terminal", "brightness"))
        self.config.add_callback(self.brightness, "terminal", "brightness")
        handler = graypy.GELFHandler(
            self.config.get("graylog", "hostname"),
            int(self.config.get("graylog", "port"))
        )
        try:
            handler.setLevel(
                logging.getLevelName(
                    self.config.get("graylog", "level")
                )
            )
        except ValueError:
            handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('[%(terminal)s] %(asctime)s - %(name)s - %(levelname)s %(funcName)s:%(lineno)d - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
        filter = ContextFilter(self.config.get("api", "terminal"))
        logger.addFilter(filter)
        return self.root

    def flip_request(self, *args):
        screen = self.root.manager.get_screen("RequestCard")
        screen.icon.source = next(self.request_icons)

    def on_start(self):
        logger.info("App starting")
        sounddetection = self.config.getint("terminal", "sounddetection")
        bounce = self.config.getint("terminal", "screensaver")
        logger.info(f"Detecting sound on PIN {sounddetection}")
        GPIO.setmode(GPIO.BOARD)
        GPIO.setup(sounddetection, GPIO.IN)
        GPIO.add_event_detect(
            sounddetection, GPIO.RISING, self.activate, bouncetime=5000
        )
        self.root.logo.bind(on_touch_down=self.logo_touch)

    def on_stop(self):
        logger.info("App stopping")

    def logo_touch(self, element, touch):
        if not self.root.logo.collide_point(*touch.pos):
            return
        logger.debug(f"Touch on logo")
        view = ModalView(size_hint=(0.8, 0.5))
        text = '\n'.join([f'{n}: {a}' for n, a in self.network.items()])
        view.add_widget(Label(text=text))
        view.open()

    def screensaver(self, dt):
        delta = timedelta(seconds=self.config.getint("terminal", "screensaver"))
        if datetime.now() - self.active < delta:
            return
        logger.debug(f"Activating screensaver")
        run(("/usr/bin/xset", "dpms", "force", "off"))

    def activate(self, *args):
        logger.debug(f"Activating display")
        self.active = datetime.now()
        run(("/usr/bin/xset", "dpms", "force", "on"))

    def brightness(self, section, key, value):
        if int(value) < 0 or int(value) > 255:
            return
        with open("/sys/class/backlight/rpi_backlight/brightness", "w") as f:
            f.write(value)

    def update_network(self, *args):
        for iface in interfaces():
            if iface == 'lo':
                continue
            self.network[iface] = []
            ifs = ifaddresses(iface)
            for fam in (AF_LINK, AF_INET, AF_INET6):
                addr = [a.get('addr') for a in ifs.get(fam, []) if a.get('addr')]
                self.network[iface].extend(addr)
        logger.debug(f"New network addresses: {self.network}")

    def update_time(self, *args):
        self.time = datetime.now().strftime("%c")

    def fetch_token(self, *args):
        logger.info(f"Checking token")
        url = furl(self.config.get("api", "base_url"))
        url.path /= "auth/token/"
        req = UrlRequest(
            str(url),
            method="POST",
            req_headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            req_body=json.dumps(
                {
                    "username": self.config.get("api", "username"),
                    "password": self.config.get("api", "password"),
                }
            ),
            on_success=self.handle_token,
            on_error=self.handle_token_error,
            on_failure=self.handle_token_error,
            timeout=10,
        )

    def handle_token(self, request, result):
        token = result.get("token")
        if self.token != token:
            logger.info("Received new token")
            self.token = token
            self.reset()

    def handle_token_error(self, request, result):
        logger.error("Failed to fetch new token")
        self.token = None
        self.root.manager.current = "Maintainance"

    def upload_config(self, *args):
        if self.token is None:
            return
        data = {
            "interfaces": {iface: {fam: ifaddresses(iface).get(fam) for fam in (AF_INET, AF_INET6, AF_LINK)} for iface in interfaces() if iface != "lo"},
            "gateways": gateways(),
            "platform": platform(),
            "python": python_version(),
            "version": ".".join(map(str, __version__)),
            "datetime": datetime.now().astimezone().isoformat(),
        }
        logger.debug(f"Uploading config: {data}")
        url = furl(self.config.get("api", "base_url"))
        url.path /= "v1/attendance/terminal/"
        url.path /= str(self.config.getint("api", "terminal"))
        url.path /= "/"
        req = UrlRequest(
            str(url),
            method="PATCH",
            req_headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {self.token}",
            },
            req_body=json.dumps(
                {
                    "config": data
                }
            ),
            timeout=10,
        )

    def upload_screenshot(self, *args):
        if self.config.getint("api", "screenshots") != 1:
            return
        if self.token is None:
            return
        logger.debug(f"Taking screenshot")
        try:
            proc = run(("/usr/bin/import", "-window", "root", "png:-"), capture_output=True, timeout=5, check=True)
        except (TimeoutExpired, CalledProcessError):
            logger.error(f"Failed to take screenshot")
            return
        url = furl(self.config.get("api", "base_url"))
        url.path /= "v1/attendance/terminal/"
        url.path /= str(self.config.getint("api", "terminal"))
        url.path /= "/"
        req = UrlRequest(
            str(url),
            method="PATCH",
            req_headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {self.token}",
            },
            req_body=json.dumps(
                {
                    "screen": b64encode(proc.stdout).decode('ascii')
                }
            ),
            timeout=10,
        )

    def select(self, value):
        logger.info(value)

    def check_card(self, *args):
        if self.queue.empty():
            logger.debug("Queue is empty")
            return
        while not self.queue.empty():
            uid, data = self.queue.get()
            logger.info(f"Queue pop: {uid}")
        self.activate()

        if data is not None:
            key = ''.join([f'{i:02X}' for i in data])
            keys = [i.ljust(len(key), '0') for i in self.config.get("terminal", "admin_keys").split(',')]
            if key in keys:
                logger.warning(f"UID {uid} activated admin panel")
                if self.root.manager.current == "Settings":
                    self.reset()
                else:
                    self.root.manager.current = "Settings"
                return
        if not self.token:
            logger.warning("No token present")
            return

        self.reset()
        self.cardid = "".join([("%X" % t).zfill(2) for t in uid[:4]])
        logger.info(f"Using cardid: {self.cardid}")

        self.root.manager.current = "Preflight"

        logger.info(f"Calling preflight for UID: {uid}")
        url = furl(self.config.get("api", "base_url"))
        url.path /= "attendance/"
        url.path /= str(self.config.getint("api", "terminal"))
        url.path /= self.cardid
        url.path /= "/"
        logger.info(f"Requesting URL: {url}")
        logger.info(self.token)
        req = UrlRequest(
            str(url),
            method="GET",
            req_headers={
                "Accept": "application/json",
                "Authorization": f"Token {self.token}",
            },
            on_success=self.handle_preflight,
            on_error=self.handle_network_error,
            on_failure=self.handle_failure,
            on_redirect=self.handle_network_error,
            timeout=10,
        )

    def handle_preflight(self, request, result):
        logger.info(f"preflight received: {result}")
        data = result.get("data", [])
        questions = [isinstance(s, dict) for s in data]
        if not any(questions):
            logger.info(f"No questions in preflight: {data}")
            self.clock()
            return
        self.questions = data
        self.answers = {}
        logger.info(f"Questions in preflight: {data}")
        self.ask()

    def ask(self):
        self.question = self.questions.pop()
        logger.info(f"Showing question: {self.question}")
        screen = self.root.manager.get_screen("Questions")
        screen.answers.add_widget(QuestionLabel(text=self.question.get("question")))
        for answer, text in self.question.get("options").items():
            screen.answers.add_widget(AnswerButton(data=answer, text=text))
        self.resetable_timers.append(Clock.schedule_once(self.reset, 10))
        self.root.manager.current = "Questions"

    def answer(self, answer):
        logger.info(f"Answer selected: {self.question.get('id')} {answer}")
        self.answers[self.question.get("id")] = answer
        screen = self.root.manager.get_screen("Questions")
        screen.answers.clear_widgets()
        if self.questions:
            self.ask()
        logger.info("No more questions")
        self.clock()

    def handle_failure(self, request, result):
        pp = pprint.PrettyPrinter(indent=4)
        logger.error(pp.pformat(request.resp_status))
        logger.error(pp.pformat(result))
        screen = self.root.manager.get_screen("Error")
        if request.resp_status == 404:
            # screen.message.text = "Unbekannte Karte"
            screen.message.text = result.get("detail", "Unbekannte Karte")
            screen.image.source = "images/question-circle.png"
        else:
            screen.message.text = "Buchungsfehler"
            screen.image.source = "images/exclamation-triangle.png"
        self.resetable_timers.append(Clock.schedule_once(self.reset, 2))
        self.root.manager.current = "Error"

    def handle_network_error(self, request, error):
        logger.error(f"Network failed: {request} -> {error}")
        screen = self.root.manager.get_screen("Error")
        screen.message.text = "Netzwerkfehler"
        self.resetable_timers.append(Clock.schedule_once(self.reset, 2))
        self.root.manager.current = "Error"

    def reset(self, *args):
        for timer in self.resetable_timers:
            timer.cancel()
        self.resetable_timers = []
        if self.root.manager.current != "RequestCard":
            self.root.manager.current = "RequestCard"
        self.cardid = None
        screen = self.root.manager.get_screen("Questions")
        screen.scroller.scroll_y = 1

    def clock(self):
        for timer in self.resetable_timers:
            timer.cancel()
        self.root.manager.current = "Clock"
        url = furl(self.config.get("api", "base_url"))
        url.path /= "attendance/"
        url.path /= str(self.config.getint("api", "terminal"))
        url.path /= self.cardid
        url.path /= "/"
        req = UrlRequest(
            str(url),
            method="POST",
            req_headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Token {self.token}",
            },
            req_body=json.dumps(self.answers),
            on_success=self.handle_clock,
            on_error=self.handle_network_error,
            on_failure=self.handle_network_error,
            on_redirect=self.handle_network_error,
            timeout=10,
        )

    def handle_clock(self, request, result):
        logger.info(f"Confirmation: {result}")
        screen = self.root.manager.get_screen("Confirmation")
        logger.info(result.get("data"))
        screen.message.text = "\n".join(result.get("data"))
        self.root.manager.current = "Confirmation"
        self.resetable_timers.append(Clock.schedule_once(self.reset, 3))


if __name__ == "__main__":
    queue = Queue()
    reader = CardReader()
    process = Process(target=reader, args=(queue,))
    process.start()
    AttendanceApp(queue).run()
    reader.stop()
    process.join()

# vim: tabstop=8 expandtab shiftwidth=4 softtabstop=4
