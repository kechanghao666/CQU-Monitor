"""配置只保存连接参数；真实串口仅在用户开始硬件实验后打开。"""

from copy import deepcopy
import json
import math
import re
from pathlib import Path

ROLES = ("voltage_left", "voltage_right", "current")
DEFAULT_PORTS = dict(zip(ROLES, ("COM1", "COM2", "COM3")))
ROLE_KINDS = dict(zip(ROLES, ("电压A", "电压B", "电流")))
ROLE_NAMES = {
    "voltage_left": "电压表1（1—15）",
    "voltage_right": "电压表2（16—30）",
    "current": "电流表",
    "controller_left": "左组脉冲控制器",
    "controller_right": "右组脉冲控制器",
}
CONTROLLER_GROUPS = ("left", "right")


def validate_countdown_scale(value):
    # 1秒量化无法以现有保守证据稳定证明仅2秒宽的窗口，不能冒充兼容。
    """限制倒计时量化精度，避免把无法证明有效窗口的设备状态当作可靠同步。"""
    if type(value) not in (int, float) or value not in (0.01, 0.1):
        raise ValueError(
            "控制器倒计时单位仅支持0.01或0.1秒；1秒及其他精度不受支持，"
            "无法可靠确认有效窗口，请核对设备参数"
        )
    return value


def default_controllers():
    # 通讯地址按说明书表格默认直读；兼容选项只影响读报文，不写设备参数。
    """生成两份独立控制器参数；这些值是读取默认配置，不会写入设备。"""
    return {
        group: dict(
            port=f"COM{index}",
            baudrate=9600,
            unit=1,
            parity="E",
            stopbits=1,
            timeout=0.1,
            function=3,
            address_offset=0,
            countdown_scale=0.01,
        )
        for group, index in zip(CONTROLLER_GROUPS, (4, 5))
    }


def device_caption(role, port=None):
    """用实际配置端口生成界面名称，仪表角色与控制器角色保持分开。"""
    if role in ("controller_left", "controller_right"):
        group = role.removeprefix("controller_")
        name = (port or default_controllers()[group]["port"]).strip().upper()
        return f"{name}（控制器{'A' if group == 'left' else 'B'}）"
    name = (port or DEFAULT_PORTS[role]).strip().upper()
    return f"{name}（{ROLE_KINDS[role]}）"


def default_settings():
    """返回可独立修改的默认配置；启动默认待机，阈值默认不启用。"""
    return {
        "mode": "hardware",
        "synchronization": "controller",
        "controllers": default_controllers(),
        "step_seconds": 6,
        "sample_interval": 1,
        "valid_start": 2,
        "valid_end": 4,
        "plot_points": 2000,
        "voltage_low": None,
        "voltage_high": None,
        "current_low": None,
        "current_high": None,
        "ports": {
            role: {"port": DEFAULT_PORTS[role], "baudrate": 9600, "unit": 1, "timeout": 0.1}
            for role in ROLES
        },
    }


def validate_timing(settings):
    """软件与设备统一以0.01秒表达输出宽度；采样仍保持每秒一次。"""
    values = [
        settings.get(key, default)
        for key, default in (("step_seconds", 6), ("valid_start", 2), ("valid_end", 4))
    ]
    for value in values:
        if (
            type(value) not in (int, float)
            or not math.isfinite(value)
            or not math.isclose(value * 100, round(value * 100), abs_tol=1e-7, rel_tol=0)
        ):
            raise ValueError("输出时长与有效时间须为有限数值，精确到0.01秒")
    width, start, end = values
    if not 0.01 <= width <= 99.99:
        raise ValueError("脉冲输出时长须为0.01—99.99秒")
    if not 0 <= start < end <= width:
        raise ValueError("有效时间须满足：0 ≤ 起始时间 < 结束时间 ≤ 脉冲输出时长")
    interval = settings.get("sample_interval", 1)
    if type(interval) not in (int, float) or interval != 1:
        raise ValueError("采样频率固定为每秒一次")
    return width, start, end


def validate_settings(settings, for_start=False):
    """检查时序、阈值及总线配置；开始前额外拒绝缺失或冲突的串口。"""
    if settings.get("mode") != "hardware":
        raise ValueError("正式版仅支持真实仪表采集")
    source = settings.get("synchronization", "manual")
    if source not in ("manual", "controller"):
        raise ValueError("同步方式必须为控制器或手动")
    validate_timing(settings)
    if (
        not isinstance(settings.get("plot_points"), int)
        or not 100 <= settings["plot_points"] <= 20000
    ):
        raise ValueError("实时曲线点数必须为100—20000")
    for prefix in ("voltage", "current"):
        low, high = (settings.get(prefix + suffix) for suffix in ("_low", "_high"))
        for value in (low, high):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
            ):
                raise ValueError("报警阈值必须为有限数值或留空")
        if low is not None and high is not None and low > high:
            raise ValueError("报警下限不能高于上限")
    ports = settings.get("ports", {})
    names = []
    for role in ROLES:
        port = ports.get(role, {})
        if not isinstance(port.get("port"), str):
            raise ValueError("串口名称必须为文字")
        if port.get("baudrate") not in (1200, 2400, 4800, 9600, 19200, 38400, 57600, 115200):
            raise ValueError("波特率不受支持")
        if not isinstance(port.get("unit"), int) or not 1 <= port["unit"] <= 247:
            raise ValueError("设备站号必须为1—247")
        timeout = port.get("timeout")
        if (
            not isinstance(timeout, (float, int))
            or not math.isfinite(timeout)
            or not 0.02 <= timeout <= 2
        ):
            raise ValueError("读取超时必须为0.02—2秒")
        names.append(port["port"].strip().upper())
    if for_start and settings["mode"] == "hardware":
        if not all(names):
            raise ValueError("硬件采集前，请设置三台仪表对应的串口")
        if len(set(names)) != 3:
            raise ValueError("三个串口不能重复，请核对USB转串口设备")
    # 手动模式也不能保留不支持的旧精度，以免切回控制器时静默改成默认项。
    for group in CONTROLLER_GROUPS:
        profile = settings.get("controllers", {}).get(group, {})
        if source == "controller" or "countdown_scale" in profile:
            validate_countdown_scale(profile.get("countdown_scale"))
    if source == "controller":
        buses = {}
        for group in CONTROLLER_GROUPS:
            profile = settings.get("controllers", {}).get(group, {})
            name = profile.get("port", "")
            if not isinstance(name, str) or not re.fullmatch(r"COM[1-9]\d*", name.strip().upper()):
                raise ValueError("请填写两台控制器的实际串口，如COM4")
            name = name.strip().upper()
            if name in names:
                raise ValueError("控制器不能占用三个仪表已使用的串口")
            if profile.get("baudrate") not in (4800, 9600, 19200, 38400, 57600, 115200):
                raise ValueError(
                    "控制器波特率过低或不受支持，须为4800及以上；低速无法满足状态读取时限"
                )
            if type(profile.get("unit")) is not int or not 1 <= profile["unit"] <= 99:
                raise ValueError("控制器站号必须为1—99")
            if profile.get("parity") not in ("E", "O", "N") or profile.get("stopbits") not in (
                1,
                2,
            ):
                raise ValueError("控制器奇偶校验或停止位不正确")
            timeout = profile.get("timeout")
            if (
                type(timeout) not in (int, float)
                or not math.isfinite(timeout)
                or not 0.02 <= timeout <= 0.2
            ):
                raise ValueError("控制器读取超时必须为0.02—0.2秒，避免阻塞另一组状态同步")
            if profile.get("function") not in (3, 4) or profile.get("address_offset") not in (
                -1,
                0,
            ):
                raise ValueError("控制器只支持03/04读取及0/-1地址偏移")
            serial = tuple(profile[k] for k in ("baudrate", "parity", "stopbits", "timeout"))
            if name in buses:
                previous_serial, units = buses[name]
                if profile["unit"] in units:
                    raise ValueError("同一控制器总线的站号不能重复")
                if serial != previous_serial:
                    raise ValueError("同一控制器总线的串口参数必须一致")
                units.add(profile["unit"])
            else:
                buses[name] = serial, {profile["unit"]}
    return settings


def load_settings(path: Path):
    """读取配置并补全旧版缺省字段；文件缺失采用默认，文件损坏则明确报错。"""
    path = Path(path)
    if not path.exists():
        return default_settings()
    try:
        loaded = json.loads(path.read_text(encoding="utf-8-sig"))
        settings = default_settings()
        settings.update(loaded)
        # 兼容旧版设置文件，但不保留旧版数据发生器或倍速配置。
        settings["mode"] = "hardware"
        settings.pop("simulation_speed", None)
        settings["ports"] = deepcopy(default_settings()["ports"])
        settings["controllers"] = default_controllers()
        for group, value in loaded.get("controllers", {}).items():
            if group in CONTROLLER_GROUPS:
                settings["controllers"][group].update(value)
        for role, value in loaded.get("ports", {}).items():
            if role in ROLES:
                settings["ports"][role].update(value)
        for role in ROLES:
            if not settings["ports"][role]["port"].strip():
                settings["ports"][role]["port"] = DEFAULT_PORTS[role]
        return validate_settings(settings)
    except (ValueError, TypeError, AttributeError, OSError) as exc:
        raise ValueError(f"配置文件无法读取：{path}；{exc}") from exc


def save_settings(path: Path, settings):
    """校验后在同目录临时写入并替换配置，避免中途失败留下半个JSON。"""
    validate_settings(settings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(
        json.dumps(settings, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8"
    )
    temporary.replace(path)
