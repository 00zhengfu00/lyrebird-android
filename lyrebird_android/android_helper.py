import subprocess
import threading
import os
import sys
import json
import time
import codecs
from lyrebird import context
from lyrebird import get_logger
import lyrebird
from . import config

"""
Android Debug Bridge command helper

Basic ADB command for device_service and API
"""

logger = get_logger('Android')

here = os.path.dirname(__file__)
adb = None
static = os.path.abspath(os.path.join(here, 'static'))
storage = lyrebird.get_plugin_storage()

tmp_dir = os.path.abspath(os.path.join(storage, 'tmp'))
anr_dir = os.path.abspath(os.path.join(storage, 'anr'))
crash_dir = os.path.abspath(os.path.join(storage, 'crash'))

if not os.path.exists(tmp_dir):
    os.makedirs(tmp_dir)

if not os.path.exists(anr_dir):
    os.makedirs(anr_dir)

if not os.path.exists(crash_dir):
    os.makedirs(crash_dir)

class ADBError(Exception):
    pass


class AndroidHomeError(Exception):
    pass


def check_android_home():
    global adb
    android_home = os.environ.get('ANDROID_HOME')
    if not android_home or android_home == '':
        raise AndroidHomeError('Not set env : ANDROID_HOME')
    if not os.path.exists(android_home):
        raise AndroidHomeError('ANDROID_HOME %s not exists' % android_home)
    if not os.path.isdir(android_home):
        raise AndroidHomeError('ANDROID_HOME %s is not a dir' % android_home)
    if sys.platform == 'win32':
        adb = os.path.abspath(os.path.join(android_home, 'platform-tools/adb.exe'))
    elif sys.platform == 'darwin' or sys.platform == 'linux':
        adb = os.path.abspath(os.path.join(android_home, 'platform-tools/adb'))
    else:
        raise ADBError('Unsupported platform')


class App:

    def __init__(self, package):
        self.package = package
        self.launch_activity = None
        self.version_name = None
        self.version_code = None
        self.raw = None

    @classmethod
    def from_raw(cls, package, raw_data):
        app = cls(package)
        app.raw = raw_data
        lines = raw_data.split('\n')

        actionMAIN_line_num = None

        for index, line in enumerate(lines):
            if 'versionCode' in line:
                app.version_code = line.strip().split(' ')[0]
            if 'versionName' in line:
                app.version_name = line.strip().split('=')[1]
            if 'android.intent.action.MAIN' in line and not actionMAIN_line_num:
                actionMAIN_line_num = index
            if app.version_name and app.version_code and actionMAIN_line_num:
                break

        def get_activity_name(line):
            info_list = line.strip().split(' ')
            return info_list[1]

        package_name_line = lines[actionMAIN_line_num + 1]
        app.launch_activity = get_activity_name(package_name_line)

        return app


class Device:

    def __init__(self, device_id):
        self.device_id = device_id
        self.state = None
        self.product = None
        self.model = None
        self._log_process = None
        self._log_cache = []
        self._log_crash_cache = []
        self._log_file = None
        self._log_filtered_file = None
        self._crash_filtered_file = None
        self._anr_filtered_file = None
        self._screen_shot_file = os.path.abspath(os.path.join(tmp_dir, 'android_screenshot_%s.png' % self.device_id))
        self._anr_file = None
        self._crash_file_list = []
        self._device_info = None
        self._app_info = None
        self.start_catch_log = False

    @property
    def log_file(self):
        return self._log_file
    
    @property
    def log_filtered_file(self):
        return self._log_filtered_file

    @property
    def crash_filtered_file(self):
        return self._crash_filtered_file

    @property
    def anr_filtered_file(self):
        return self._anr_filtered_file

    @property
    def screen_shot_file(self):
        return self._screen_shot_file

    @property
    def anr_file(self):
        return self._anr_file

    @property
    def crash_file_list(self):
        return self._crash_file_list

    @classmethod
    def from_adb_line(cls, line):
        device_info = [info for info in line.split(' ') if info]
        if len(device_info) < 2:
            raise ADBError(f'Read device info line error. {line}')
        _device = cls(device_info[0])
        _device.state = device_info[1]
        for info in device_info[2:]:
            info_kv = info.split(':')
            if len(info_kv) >= 2:
                setattr(_device, info_kv[0], info_kv[1])
            else:
                logger.error('Read device info error: unknown format', info_kv)
        return _device

    def install(self, apk_file):
        subprocess.run(f'{adb} -s {self.device_id} install -r {apk_file}', shell=True)

    def push(self, src, dst):
        subprocess.run(f'{adb} -s {self.device_id} push {src} {dst}')

    def pull(self, src, dst):
        subprocess.run(f'{adb} -s {self.device_id} pull {src} {dst}')

    def start_log(self):
        self.stop_log()

        log_file_name = 'android_log_%s.log' % self.device_id
        self._log_file = os.path.abspath(os.path.join(tmp_dir, log_file_name))

        p = subprocess.Popen(f'{adb} -s {self.device_id} logcat', shell=True, stdout=subprocess.PIPE)
        
        conf = config.load()
        package_name = conf.package_name
        pid_target = []

        p2 = subprocess.run(f'{adb} -s {self.device_id} shell ps | grep {package_name}', shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        
        pid_list = p2.stdout.decode().split('\n')
        for p2_line in pid_list:
            if p2_line:
                pid_target.append(str(p2_line).strip().split( )[1])
        
        log_filtered_file_name = 'android_log_%s_%s.log' % (self.device_id, package_name)
        self._log_filtered_file = os.path.abspath(os.path.join(tmp_dir, log_filtered_file_name))

        crash_filtered_file_name = 'android_crash_%s_%s.log' % (self.device_id, package_name)
        self._crash_filtered_file = os.path.abspath(os.path.join(crash_dir, crash_filtered_file_name))

        anr_filtered_file_name = 'android_anr_%s_%s.log' % (self.device_id, package_name)
        self._anr_filtered_file = os.path.abspath(os.path.join(anr_dir, anr_filtered_file_name))
        
        def log_handler(logcat_process):
            log_file = codecs.open(self._log_file, 'w', 'utf-8')
            log_filtered_file = codecs.open(self._log_filtered_file, 'w', 'utf-8')
            crash_filtered_file = codecs.open(self._crash_filtered_file, 'w', 'utf-8')
            anr_filtered_file = codecs.open(self._anr_filtered_file, 'w', 'utf-8')

            while True:
                line = logcat_process.stdout.readline()

                if not line:
                    context.application.socket_io.emit('log', self._log_cache, namespace='/android-plugin')
                    log_file.close()
                    log_filtered_file.close()
                    crash_filtered_file.close()
                    anr_filtered_file.close()
                    return

                if self.log_filter(line, pid_target):
                    log_filtered_file.writelines(line.decode(encoding='UTF-8', errors='ignore'))
                    log_filtered_file.flush()
                
                if self.crash_checker(line) and self.log_filter(line, pid_target):
                    item = {'name':'crash', 'message':str(line.decode(encoding='UTF-8', errors='ignore'))}
                    lyrebird.publish('device', item)
                    crash_filtered_file.writelines(line.decode(encoding='UTF-8', errors='ignore'))
                    crash_filtered_file.flush()

                if self.anr_checker(line):
                    anr_file_name = os.path.join(anr_dir, 'android_anr_%s.log' % self.device_id)
                    with codecs.open(anr_file_name, 'r', 'utf-8') as f:
                        anr_headline = f.readline()
                        anr_headline = f.readline()
                    
                    p4 = subprocess.run(f'{adb} -s {self.device_id} shell ps | grep {package_name}', shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                    pid_list = p4.stdout.decode().split('\n')
                    for p2_line in pid_list:
                         if p2_line:
                            pid_target.append(str(p2_line).strip().split( )[1])
                    if str(anr_headline).strip().split()[2] in pid_target:
                        item = {'name':'anr', 'message':str(line.decode(encoding='UTF-8', errors='ignore'))}
                        lyrebird.publish('device', item)
                        subprocess.run(f'{adb} -s {self.device_id} pull "/data/anr/traces.txt" {self._anr_filtered_file}', shell=True, stdout=subprocess.PIPE)
                        
                self._log_cache.append(line.decode(encoding='UTF-8', errors='ignore'))

                if len(self._log_cache) >= 10:
                    context.application.socket_io.emit('log', self._log_cache, namespace='/android-plugin')
                    log_file.writelines(self._log_cache)
                    log_file.flush()
                    self._log_cache = []
        threading.Thread(target=log_handler, args=(p,)).start()

    def log_filter(self, line, pid_target):
        if line:
            pid_cur = str(line).strip().split()[2]
            if pid_cur in pid_target:
                return True
            else:
                return False

    def crash_checker(self, line):
        crash_log_path = os.path.join(crash_dir, 'android_crash_%s.log' % self.device_id)

        if str(line).find('FATAL EXCEPTION') > 0:
            self.start_catch_log = True
            self._log_crash_cache.append(line.decode(encoding='UTF-8', errors='ignore'))
            return True
        elif str(line).find('AndroidRuntime') > 0 and self.start_catch_log:
            self._log_crash_cache.append(line.decode(encoding='UTF-8', errors='ignore'))
            return True
        else:
            self.start_catch_log = False
            with codecs.open(crash_log_path, 'w', 'utf-8') as f:
                f.write(''.join(self._log_crash_cache))
            return False
        

    def anr_checker(self, line):
        if str(line).find('ANR') > 0 and str(line).find('ActivityManager') > 0:
            self.get_anr_log()
            return True
        else:
            return False

    def get_anr_log(self):
        anr_file_name = os.path.join(anr_dir, 'android_anr_%s.log' % self.device_id)
        p = subprocess.run(f'{adb} -s {self.device_id} pull "/data/anr/traces.txt" {anr_file_name}', shell=True, stdout=subprocess.PIPE)
        if p.returncode == 0:
            self._anr_file = os.path.abspath(anr_file_name)

    @property
    def device_info(self):
        if not self._device_info:
            self._device_info = self.get_properties()
        return self._device_info

    def get_properties(self):
        p = subprocess.run(f'{adb} -s {self.device_id} shell getprop', shell=True, stdout=subprocess.PIPE)
        if p.returncode == 0:
            return p.stdout.decode().split('\n')

    def package_info(self, package_name):
        p = subprocess.run(f'{adb} -s {self.device_id} shell dumpsys package {package_name}', shell=True,
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode != 0:
            raise ADBError(p.stderr.decode())
        return App.from_raw(package_name, p.stdout.decode())

    def package_meminfo(self, package_name):
        p = subprocess.run(f'{adb} -s {self.device_id} shell dumpsys meminfo {package_name}', shell=True, 
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if p.returncode == 0:
            return p.stdout.decode().split('\n')

    def device_cpuinfo(self):
        p = subprocess.run(f'{adb} -s {self.device_id} shell dumpsys cpuinfo', shell=True, stdout=subprocess.PIPE)
        if p.returncode == 0:
            return p.stdout.decode().split('\n')

    def stop_log(self):
        if self._log_process:
            self._log_process.kill()
            self._log_process = None

    def take_screen_shot(self):
        p = subprocess.run(f'{adb} -s {self.device_id} exec-out screencap -p > {tmp_dir}/android_screenshot_{self.device_id}.png', shell=True)
        if p.returncode == 0:
            return os.path.abspath(os.path.join(tmp_dir, 'android_screenshot_%s.png' % self.device_id))

    def start_app(self, start_activity, ip, port):
        p = subprocess.run(f'{adb} -s {self.device_id} shell am start -n {start_activity} --es mock http://{ip}:{port}/mock/ --es closeComet true', shell=True)
        return True if p.returncode == 0 else False

    def stop_app(self, package_name):
        p = subprocess.run(f'{adb} -s {self.device_id} shell am force-stop {package_name}', shell=True)
        return True if p.returncode == 0 else False

    def to_dict(self):
        device_info = {k: self.__dict__[k] for k in self.__dict__ if not k.startswith('_')}
        # get additional device info
        prop_lines = self.device_info
        if not prop_lines:
            return device_info

        for line in prop_lines:
            # 基带版本
            if 'ro.build.expect.baseband' in line:
                baseband = line[line.rfind('[')+1:line.rfind(']')].strip()
                device_info['baseBand'] = baseband
            # 版本号
            if 'ro.build.id' in line:
                build_id = line[line.rfind('[') + 1:line.rfind(']')].strip()
                device_info['buildId'] = build_id
            # Android 版本
            if 'ro.build.version.release' in line:
                build_version = line[line.rfind('[') + 1:line.rfind(']')].strip()
                device_info['releaseVersion'] = build_version
        return device_info


def devices():
    check_android_home()
    res = subprocess.run(f'{adb} devices -l', shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    output = res.stdout.decode()
    err_str = res.stderr.decode()
    # ADB命令执行异常
    if len(output) <= 0 < len(err_str):
        print('Get devices list error', err_str)
        return []

    lines = [line for line in output.split('\n') if line]
    online_devices = {}
    if len(lines) <= 1:
        # print('Not found any devices')
        return online_devices

    for line in lines[1:]:
        device = Device.from_adb_line(line)
        online_devices[device.device_id] = device
    return online_devices