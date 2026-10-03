"""
多版本订阅（MultiVersionSubscribe）

按订阅自身的「订阅站点 + 过滤规则组 + 包含/排除」搜索资源，
再使用插件配置的「画质规格」逐个规格独立下载：

- 每个规格单独判断、单独记录，不受出种顺序、洗版优先级限制；
- 同时保留 1080p / 4K / 杜比视界 等多个版本；
- 下载状态记录在插件内部，不依赖媒体库（下载后删除、不入库、不刮削同样可用）。

适用场景：想要某个剧的多个版本，但 MoviePilot 原生订阅只会下载「一个最佳版本」。

v1.2.0 更新：

- 执行周期支持「跟随系统」（取系统「订阅搜索间隔」与「RSS 间隔」中更短的一个），也可直接选固定分钟；
- 支持「组合规格」（如 4K+DV+60FPS），组合内的条件必须同时满足才算命中；
- 支持「每个订阅单独配置规格」，未单独配置的订阅使用默认规格；
- 电视剧严格按订阅的「开始集数 / 总集数」过滤，范围之外的剧集不再下载。
"""
import re
import threading
import time
import traceback
from typing import Any, Dict, List, Optional, Set, Tuple

from apscheduler.triggers.interval import IntervalTrigger

from app.chain.download import DownloadChain
from app.chain.search import SearchChain
from app.core.config import settings
from app.core.event import Event, eventmanager
from app.core.metainfo import MetaInfo
from app.db.subscribe_oper import SubscribeOper
from app.db.systemconfig_oper import SystemConfigOper
from app.helper.downloader import DownloaderHelper
from app.helper.torrent import TorrentHelper
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, MediaType, NotificationType, SystemConfigKey, TorrentStatus

# 规格字段：名称 | 分辨率 | 质量 | 特效 | 包含 | 排除 | 大小(GB)
SPEC_FIELDS = ("name", "resolution", "quality", "effect", "include", "exclude", "size")

# 预设画质规格：(key, 名称, 过滤参数)
# 关键词取自 MoviePilot 内置过滤规则，匹配时忽略大小写
SPEC_PRESETS: Tuple[Tuple[str, str, dict], ...] = (
    ("1080p", "1080p", {"resolution": r"1080[pi]|x1080"}),
    ("4k", "4K", {"resolution": r"4k|2160p|x2160"}),
    ("720p", "720p", {"resolution": r"720[pi]|x720"}),
    ("dv", "杜比视界（Dolby Vision）", {"effect": r"Dolby[\s.]+Vision|DOVI|[\s.]+DV[\s.]+|杜比视界"}),
    ("hdr", "HDR", {"effect": r"[\s.]+HDR[\s.]+|HDR10|HDR10\+|HDRVivid"}),
    ("atmos", "杜比全景声", {"effect": r"Dolby[\s.+]+Atmos|Atmos|杜比全景[声效]"}),
    ("remux", "REMUX", {"quality": r"REMUX"}),
    ("bluray", "Blu-ray", {"quality": r"Blu-?Ray"}),
    ("webdl", "WEB-DL", {"quality": r"WEB-?DL|WEB-?RIP"}),
    ("uhd", "UHD", {"quality": r"UHD|UltraHD"}),
    ("h265", "H265/HEVC", {"quality": r"[Hx].?265|HEVC"}),
    ("cnsub", "中字", {"include": r"中字|中文字幕|简中|繁中|简体|繁体|简英|chs|cht"}),
    ("specsub", "特效字幕", {"include": r"特效"}),
    ("60fps", "60FPS", {"effect": r"60fps|60帧"}),
)

# key -> (名称, 过滤参数)
SPEC_PRESET_MAP: Dict[str, Tuple[str, dict]] = {
    key: (name, params) for key, name, params in SPEC_PRESETS
}

# 组合规格的分隔符：4K+DV+60FPS
COMBO_SEPARATORS = "+&,，、/"

# 执行周期可选项：(值, 名称)，system 表示跟随系统
INTERVAL_OPTIONS: Tuple[Tuple[str, str], ...] = (
    ("system", "跟随系统"),
    ("5", "5 分钟"),
    ("10", "10 分钟"),
    ("15", "15 分钟"),
    ("30", "30 分钟"),
    ("60", "1 小时"),
    ("120", "2 小时"),
    ("360", "6 小时"),
    ("720", "12 小时"),
    ("1440", "24 小时"),
)

# 订阅单独规格的配置项前缀：subspec_<订阅ID>
SUB_SPEC_PREFIX = "subspec_"

# 预设规格在组合规格里使用的短名称
SPEC_PRESET_SHORT: Dict[str, str] = {
    "1080p": "1080p",
    "4k": "4K",
    "720p": "720p",
    "dv": "DV",
    "hdr": "HDR",
    "atmos": "Atmos",
    "remux": "REMUX",
    "bluray": "BluRay",
    "webdl": "WEB-DL",
    "uhd": "UHD",
    "h265": "H265",
    "cnsub": "中字",
    "specsub": "特效字幕",
    "60fps": "60FPS",
}

# 预设规格的别名（key / 名称 / 名称去括号），用于在组合规格里按名字书写
SPEC_PRESET_ALIAS: Dict[str, str] = {}
for _preset_key, _preset_name, _ in SPEC_PRESETS:
    for _alias in (_preset_key, _preset_name, _preset_name.split("（")[0], _preset_name.split("(")[0]):
        _alias = (_alias or "").strip().lower()
        if _alias:
            SPEC_PRESET_ALIAS.setdefault(_alias, _preset_key)


def _col(component: str, props: dict, md: int = 6) -> dict:
    """
    生成 Vuetify 表单列
    """
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": md},
        "content": [{"component": component, "props": props}],
    }



def _btn(text: str, color: str, api: str) -> dict:
    """
    生成带接口调用的按钮
    """
    return {
        "component": "VCol",
        "props": {"cols": 12, "md": 4},
        "content": [
            {
                "component": "VBtn",
                "props": {
                    "color": color,
                    "variant": "tonal",
                    "size": "small",
                    "text": text,
                    "block": True,
                },
                "events": {
                    "click": {
                        "api": api,
                        "method": "get",
                        "params": {"apikey": settings.API_TOKEN},
                    }
                },
            }
        ],
    }


class MultiVersionSubscribe(_PluginBase):
    # 插件名称
    plugin_name = "多版本订阅"
    # 插件描述
    plugin_desc = "为指定订阅按自定义画质规格（如 1080p / 4K / 杜比视界）分别下载，多版本同时保留，不受出种顺序和洗版优先级限制。"
    # 插件图标
    plugin_icon = "torrent.png"
    # 插件版本
    plugin_version = "1.2.0"
    # 插件作者
    plugin_author = "uzzyj333"
    # 作者主页
    author_url = "https://github.com/uzzyj333"
    # 插件配置项ID前缀
    plugin_config_prefix = "multiversionsubscribe_"
    # 加载顺序
    plugin_order = 21
    # 可使用的用户级别
    auth_level = 1

    # 配置
    _enabled: bool = False
    _subscribes: List[str] = []
    _spec_presets: List[str] = []
    _specs_text: str = ""
    _combos_text: str = ""
    _sub_specs: Dict[str, List[str]] = {}
    _interval: str = "system"
    _notify: bool = False
    _auto_pause: bool = True
    _auto_delete: bool = False
    _download_all: bool = False
    _save_path: str = ""
    _downloader: str = ""
    _onlyonce: bool = False

    # 运行态
    _specs: List[dict] = []
    _running: bool = False
    # 「保存后立即运行一次」使用的定时器
    _timer: Optional[threading.Timer] = None

    def __init__(self):
        super().__init__()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # 生命周期
    # ------------------------------------------------------------------ #
    def init_plugin(self, config: dict = None):
        # 停止现有任务（「立即运行一次」的定时器）
        self.stop_service()
        self._specs = []
        if config:
            self._enabled = bool(config.get("enabled"))
            self._subscribes = [str(item) for item in (config.get("subscribes") or [])]
            self._spec_presets = [str(item) for item in (config.get("spec_presets") or [])]
            self._specs_text = config.get("specs") or ""
            self._combos_text = config.get("spec_combos") or ""
            self._interval = str(config.get("interval") or "system")
            self._sub_specs = {
                str(key)[len(SUB_SPEC_PREFIX):]: [str(item) for item in (value or [])]
                for key, value in config.items()
                if str(key).startswith(SUB_SPEC_PREFIX)
            }
            self._notify = bool(config.get("notify"))
            self._auto_pause = config.get("auto_pause", True)
            self._auto_delete = bool(config.get("auto_delete"))
            self._download_all = bool(config.get("download_all"))
            self._save_path = (config.get("save_path") or "").strip()
            self._downloader = (config.get("downloader") or "").strip()
            self._onlyonce = bool(config.get("onlyonce"))
        else:
            self._enabled = False

        if not self._enabled:
            return

        library = self.__spec_library()
        self._specs = self.__resolve_specs(self._spec_presets, library)
        if not self._specs and not self._sub_specs:
            logger.error("多版本订阅：没有配置任何画质规格，插件不会下载任何资源")
        else:
            logger.info(
                f"多版本订阅：可用规格 {list(library)}，默认规格 {[spec.get('name') for spec in self._specs]}"
            )
        if not self._subscribes:
            logger.error("多版本订阅：没有选择任何订阅，插件不会处理任何内容")

        # 立即运行一次：延迟 3 秒，避免与配置保存请求相互影响
        if self._onlyonce:
            self._onlyonce = False
            self.__update_config()
            self._timer = threading.Timer(3, self.check)
            self._timer.daemon = True
            self._timer.start()
            logger.info("多版本订阅：已安排 3 秒后立即运行一次")

    def get_state(self) -> bool:
        return self._enabled

    def stop_service(self):
        """
        停止插件服务（仅用于「保存后立即运行一次」的定时器）
        """
        try:
            if self._timer:
                self._timer.cancel()
                self._timer = None
        except Exception as err:
            logger.error(f"多版本订阅：停止定时服务出错：{err}")

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册定时服务：执行周期可跟随系统，也可在插件里直接选固定周期
        """
        if self._enabled:
            minutes = self.__interval_minutes()
            logger.info(f"多版本订阅：注册定时服务，每 {self.__human_interval(minutes)} 执行一次")
            return [{
                "id": "MultiVersionSubscribe",
                "name": f"多版本订阅（每 {self.__human_interval(minutes)}）",
                "trigger": IntervalTrigger(minutes=minutes),
                "func": self.check,
                "kwargs": {}
            }]
        return []

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        注册远程命令
        """
        return [{
            "cmd": "/mvsub",
            "event": EventType.PluginAction,
            "desc": "多版本订阅：立即搜索并下载",
            "category": "",
            "data": {"action": "mvsub_search"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [
            {
                "path": "/run",
                "endpoint": self.api_run,
                "methods": ["GET"],
                "summary": "立即搜索并下载"
            },
            {
                "path": "/resume",
                "endpoint": self.api_resume,
                "methods": ["GET"],
                "summary": "恢复被插件接管的订阅状态"
            },
            {
                "path": "/clear",
                "endpoint": self.api_clear,
                "methods": ["GET"],
                "summary": "清空下载记录与进度"
            }
        ]

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """
        处理远程命令
        """
        if not event or not self._enabled:
            return
        data = event.event_data or {}
        if data.get("action") == "mvsub_search":
            self.check()

    # ------------------------------------------------------------------ #
    # 插件 API
    # ------------------------------------------------------------------ #
    def api_run(self):
        threading.Thread(target=self.check, name="MultiVersionSubscribe-Manual", daemon=True).start()
        return {"success": True, "message": "已开始执行，请查看日志"}

    def api_resume(self):
        """
        把被插件暂停的订阅恢复为「订阅中」，并停止接管
        """
        count = 0
        for sid in self._subscribes:
            try:
                subscribe = SubscribeOper().get(int(sid))
            except Exception:
                subscribe = None
            if not subscribe or subscribe.state != "S":
                continue
            SubscribeOper().update(subscribe.id, {"state": "R"})
            count += 1
        return {"success": True, "message": f"已恢复 {count} 个订阅"}

    def api_clear(self):
        self.save_data("history", [])
        self.save_data("progress", {})
        self.save_data("downloaded", {})
        self.save_data("last_result", {})
        return {"success": True, "message": "已清空记录"}

    # ------------------------------------------------------------------ #
    # 配置页面
    # ------------------------------------------------------------------ #
    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        # 可选的订阅列表
        subscribe_items = []
        selected_subscribes: List[Dict[str, str]] = []
        try:
            for subscribe in SubscribeOper().list():
                title = f"{subscribe.name}（{subscribe.year}）" if subscribe.year else subscribe.name
                if subscribe.season:
                    title = f"{title} S{subscribe.season:02d}"
                subscribe_items.append({
                    "title": f"{title} · ID:{subscribe.id}",
                    "value": str(subscribe.id)
                })
                if str(subscribe.id) in self._subscribes:
                    selected_subscribes.append({"id": str(subscribe.id), "title": title})
        except Exception as err:
            logger.error(f"多版本订阅：读取订阅列表失败：{err}")

        # 可选的下载器
        downloader_items = [{"title": "默认下载器", "value": ""}]
        try:
            for name in DownloaderHelper().get_configs().keys():
                downloader_items.append({"title": name, "value": name})
        except Exception as err:
            logger.error(f"多版本订阅：读取下载器列表失败：{err}")

        # 可选的画质规格：预设 + 组合规格 + 自定义规格
        library = self.__spec_library()
        spec_items = [{"title": spec.get("name"), "value": key} for key, spec in library.items()]

        # 执行周期（跟随系统或固定周期）
        system_minutes = self.__system_interval_minutes()
        interval_items = [{
            "title": f"跟随系统（当前：每 {self.__human_interval(system_minutes)}）",
            "value": "system"
        }] + [
            {"title": name, "value": value} for value, name in INTERVAL_OPTIONS if value != "system"
        ]

        # 每个订阅单独的规格
        sub_spec_rows = []
        for item in selected_subscribes:
            sub_spec_rows.append({
                "component": "VRow",
                "content": [
                    _col("VSelect", {
                        "model": f"{SUB_SPEC_PREFIX}{item['id']}",
                        "label": f"{item['title']} · 单独规格（留空 = 使用默认规格）",
                        "multiple": True,
                        "chips": True,
                        "clearable": True,
                        "items": spec_items
                    }, 12),
                ]
            })

        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            _col("VSwitch", {"model": "enabled", "label": "启用插件"}, 3),
                            _col("VSwitch", {"model": "notify", "label": "发送通知"}, 3),
                            _col("VSwitch", {"model": "auto_pause", "label": "接管订阅（暂停原生搜索）"}, 3),
                            _col("VSwitch", {"model": "onlyonce", "label": "保存后立即运行一次"}, 3),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VSelect", {
                                "model": "interval",
                                "label": "执行周期",
                                "items": interval_items
                            }, 6),
                            _col("VSelect", {
                                "model": "downloader",
                                "label": "下载器",
                                "items": downloader_items
                            }, 6),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VTextField", {
                                "model": "save_path",
                                "label": "保存目录",
                                "placeholder": "留空使用媒体默认下载目录，如 /downloads/mvsub"
                            }, 12),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VSelect", {
                                "model": "subscribes",
                                "label": "要接管的订阅（可多选）",
                                "multiple": True,
                                "chips": True,
                                "clearable": True,
                                "items": subscribe_items
                            }, 12),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VSelect", {
                                "model": "spec_presets",
                                "label": "默认画质规格（可多选，选几个就下几个版本；未单独配置的订阅使用）",
                                "multiple": True,
                                "chips": True,
                                "clearable": True,
                                "items": spec_items
                            }, 12),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VTextarea", {
                                "model": "spec_combos",
                                "label": "组合规格（可选，一行一个，用 + 连接，表示必须同时满足）",
                                "rows": 2,
                                "placeholder": "每行一个组合，例如：\n"
                                               "4K+DV+60FPS\n"
                                               "4K杜比全景 = 4k+atmos"
                            }, 12),
                        ]
                    },
                    *sub_spec_rows,
                    {
                        "component": "VRow",
                        "content": [
                            _col("VTextarea", {
                                "model": "specs",
                                "label": "自定义规格（可选，上面的预设不够用时再填）",
                                "rows": 3,
                                "placeholder": "每行一个，用 | 分隔，第一个字段为名称，其余用 key=value：\n"
                                               "可用键：resolution / quality / effect / include / exclude / size（单位 GB）\n"
                                               "示例：蓝光原盘 | resolution=1080[pi] | quality=REMUX | size=>20"
                            }, 12),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VSwitch", {
                                "model": "download_all",
                                "label": "同一规格下载所有匹配资源（默认每个规格只下第一个）"
                            }, 6),
                            _col("VSwitch", {
                                "model": "auto_delete",
                                "label": "下载完成后自动删除任务及文件（危险：不可恢复）"
                            }, 6),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VAlert", {
                                "type": "info",
                                "variant": "tonal",
                                "text": f"执行周期：「跟随系统」取「设置 → 订阅」里「订阅搜索间隔」"
                                        f"与「RSS 间隔」中更短的一个（当前每 {self.__human_interval(system_minutes)}），"
                                        "也可以直接选固定周期。想立刻执行可点详情页的「立即运行」，"
                                        "或使用命令 /mvsub。"
                            }, 12),
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            _col("VAlert", {
                                "type": "info",
                                "variant": "tonal",
                                "text": "使用方式：在 MoviePilot 订阅页面只设置「订阅站点」和「过滤规则组」，"
                                        "不要设置画质/分辨率/特效；把该订阅加入上方列表，"
                                        "再为它选择「单独规格」（留空则使用上面的默认规格）。"
                                        "组合规格用 + 连接多个预设，表示必须同时满足，例如 4K+DV+60FPS。"
                                        "启用「接管订阅」后插件会把该订阅置为暂停，避免原生搜索重复下载；"
                                        "需要恢复时点击详情页的「恢复订阅状态」。"
                                        "同一个种子同时命中多个规格时只会下载一次；"
                                        "电视剧只下载订阅「开始集数 ~ 总集数」范围内的剧集。"
                                        "新加入的订阅保存后重新打开本页，才会出现它的「单独规格」下拉框。"
                            }, 12),
                        ]
                    }
                ]
            }
        ], {
            "enabled": self._enabled,
            "notify": self._notify,
            "auto_pause": self._auto_pause,
            "auto_delete": self._auto_delete,
            "download_all": self._download_all,
            "onlyonce": False,
            "interval": self._interval or "system",
            "subscribes": self._subscribes,
            "spec_presets": self._spec_presets,
            "spec_combos": self._combos_text,
            "specs": self._specs_text,
            "save_path": self._save_path,
            "downloader": self._downloader,
            **{
                f"{SUB_SPEC_PREFIX}{item['id']}": self._sub_specs.get(item["id"], [])
                for item in selected_subscribes
            },
        }

    def get_page(self) -> Optional[List[dict]]:
        last_run = self.get_data("last_run") or "尚未运行"
        last_result = self.get_data("last_result") or {}
        progress = self.get_data("progress") or {}
        history = self.get_data("history") or []
        interval_text = self.__human_interval(self.__interval_minutes())
        interval_source = "跟随系统" if not (self._interval or "").strip().isdigit() else "自定义"
        spec_text = f"{len(self._specs)} 个默认"
        if self._sub_specs:
            spec_text += f" / {len(self._sub_specs)} 个订阅单独配置"

        contents: List[dict] = [
            {
                "component": "VAlert",
                "props": {
                    "type": "success" if self._enabled else "warning",
                    "variant": "tonal",
                    "class": "mb-3",
                    "text": f"状态：{'已启用' if self._enabled else '未启用'}　"
                            f"画质规格：{spec_text}　"
                            f"接管订阅：{len(self._subscribes)} 个　"
                            f"执行周期：每 {interval_text}（{interval_source}）　"
                            f"上次运行：{last_run}"
                }
            },
            {
                "component": "VAlert",
                "props": {
                    "type": str(last_result.get("type") or "info"),
                    "variant": "tonal",
                    "class": "mb-3",
                    "text": str(last_result.get("text") or "还没有运行过，点下面的「立即运行」试试。")
                }
            },
            {
                "component": "VRow",
                "props": {"class": "mb-2"},
                "content": [
                    _btn("立即运行", "primary", "plugin/MultiVersionSubscribe/run"),
                    _btn("恢复订阅状态", "warning", "plugin/MultiVersionSubscribe/resume"),
                    _btn("清空记录", "error", "plugin/MultiVersionSubscribe/clear"),
                ]
            }
        ]

        # 进度统计
        stat_lines = []
        for sid, spec_map in progress.items():
            for spec_name, state in (spec_map or {}).items():
                stat_lines.append(
                    f"订阅 {sid} · {spec_name}：已记录种子 {len(state.get('torrents') or [])} 个，"
                    f"已记录集数 {len(state.get('episodes') or [])} 集"
                )
        contents.append({
            "component": "VCard",
            "props": {"class": "mb-3"},
            "content": [
                {"component": "VCardTitle", "text": "下载进度"},
                {"component": "VCardText", "text": "\n".join(stat_lines) if stat_lines else "暂无进度数据"}
            ]
        })

        # 最近记录
        rows = []
        for item in sorted(history, key=lambda x: x.get("time") or "", reverse=True)[:50]:
            rows.append({
                "component": "VCardText",
                "text": f"[{item.get('time')}] {item.get('subscribe')} · {item.get('spec')} · "
                        f"{item.get('site')} · {item.get('title')} · {item.get('state')}"
            })
        contents.append({
            "component": "VCard",
            "content": [
                {"component": "VCardTitle", "text": "最近下载记录（最多 50 条）"},
                *rows
            ]
        })

        return [{"component": "div", "props": {"class": "grid gap-3"}, "content": contents}]

    # ------------------------------------------------------------------ #
    # 主流程
    # ------------------------------------------------------------------ #
    def check(self):
        """
        定时/手动触发的搜索下载流程
        """
        if not self._enabled:
            self.__record_result("warning", "插件未启用，本次未执行。")
            return
        if self._running:
            logger.info("多版本订阅：上一次任务仍在执行，本次跳过")
            return
        if not self._specs and not self._sub_specs:
            logger.error("多版本订阅：没有配置任何画质规格，跳过")
            self.__record_result(
                "error", "没有配置任何画质规格，请到插件设置里选择「默认画质规格」或给订阅单独配置。"
            )
            return
        if not self._subscribes:
            logger.error("多版本订阅：没有选择任何订阅，跳过")
            self.__record_result("error", "没有选择任何订阅，请到插件设置里选择要接管的订阅。")
            return
        self._running = True
        self.save_data("last_run", time.strftime("%Y-%m-%d %H:%M:%S"))
        self.__record_result("info", "正在执行，完成后会更新这里的结果…")
        logger.info(
            f"多版本订阅：开始执行，默认规格 {len(self._specs)} 个 / 订阅 {len(self._subscribes)} 个"
        )
        try:
            subscribes = self.__get_target_subscribes()
            if not subscribes:
                logger.info("多版本订阅：没有需要处理的订阅")
                self.__record_result("warning", "没有找到可处理的订阅（订阅可能已被删除）。")
                return
            total = 0
            details: List[str] = []
            for subscribe in subscribes:
                try:
                    added = self.__process_subscribe(subscribe)
                    total += added
                    details.append(f"{subscribe.name} 新增 {added} 个任务")
                except Exception as err:
                    logger.error(
                        f"多版本订阅：处理订阅「{getattr(subscribe, 'name', '')}」出错："
                        f"{err} - {traceback.format_exc()}"
                    )
                    details.append(f"{getattr(subscribe, 'name', '')} 出错（详见日志）")
            if self._auto_delete:
                try:
                    self.__auto_delete()
                except Exception as err:
                    logger.error(f"多版本订阅：自动删除任务出错：{err} - {traceback.format_exc()}")
            if self._notify and total:
                self.post_message(
                    mtype=NotificationType.Plugin,
                    title="【多版本订阅】",
                    text=f"本次共添加 {total} 个下载任务"
                )
            logger.info(f"多版本订阅：本次共添加 {total} 个下载任务")
            self.__record_result(
                "success" if total else "info",
                f"本次新增 {total} 个下载任务；" + "；".join(details)
            )
        except Exception as err:
            logger.error(f"多版本订阅：执行出错：{err} - {traceback.format_exc()}")
            self.__record_result("error", f"执行出错：{err}（详见日志）")
        finally:
            self._running = False

    def __process_subscribe(self, subscribe) -> int:
        """
        处理单个订阅，返回添加的下载任务数量
        """
        # 接管订阅：暂停原生搜索，避免重复下载
        if self._auto_pause and subscribe.state != "S":
            try:
                SubscribeOper().update(subscribe.id, {"state": "S"})
                logger.info(f"多版本订阅：已将订阅「{subscribe.name}」置为暂停，由插件接管")
            except Exception as err:
                logger.warn(f"多版本订阅：暂停订阅「{subscribe.name}」失败：{err}")

        # 识别媒体
        meta = self.__build_meta(subscribe)
        if not meta or not meta.name:
            logger.warn(f"多版本订阅：订阅「{subscribe.name}」无法构造识别信息")
            return 0
        mediainfo = self.chain.recognize_media(
            meta=meta,
            mtype=meta.type,
            tmdbid=subscribe.tmdbid,
            doubanid=subscribe.doubanid,
            bangumiid=getattr(subscribe, "bangumiid", None),
            anilistid=getattr(subscribe, "anilistid", None),
            episode_group=getattr(subscribe, "episode_group", None),
            cache=True
        )
        if not mediainfo:
            logger.warn(f"多版本订阅：未识别到媒体信息：{subscribe.name}")
            return 0

        # 订阅站点 + 订阅自己的过滤规则组（回退逻辑与宿主原生订阅保持一致）
        sites = self.__get_sub_sites(subscribe)
        if subscribe.best_version:
            rule_groups = list(getattr(subscribe, "filter_groups", None) or []) \
                or SystemConfigOper().get(SystemConfigKey.BestVersionFilterRuleGroups) or []
        else:
            rule_groups = list(getattr(subscribe, "filter_groups", None) or []) \
                or SystemConfigOper().get(SystemConfigKey.SubscribeFilterRuleGroups) or []
        base_params = self.__base_filter_params(subscribe)
        custom_words = subscribe.custom_words.split("\n") if subscribe.custom_words else None

        logger.info(
            f"多版本订阅：开始搜索「{subscribe.name}」，站点={sites}，规则组={rule_groups}，"
            f"附加参数={base_params}"
        )
        contexts = SearchChain().process(
            mediainfo=mediainfo,
            keyword=subscribe.keyword,
            no_exists=None,
            sites=sites,
            rule_groups=rule_groups,
            area="imdbid" if subscribe.search_imdbid else "title",
            custom_words=custom_words,
            filter_params=base_params
        ) or []
        if not contexts:
            logger.info(f"多版本订阅：「{subscribe.name}」未搜索到资源")
            return 0
        logger.info(f"多版本订阅：「{subscribe.name}」共搜索到 {len(contexts)} 个候选资源")

        specs = self.__specs_for(subscribe)
        if not specs:
            logger.warn(f"多版本订阅：订阅「{subscribe.name}」没有可用的画质规格，跳过")
            return 0
        logger.info(
            f"多版本订阅：「{subscribe.name}」本次使用的规格 {[spec.get('name') for spec in specs]}"
        )

        count = 0
        for spec in specs:
            try:
                count += self.__download_spec(subscribe, mediainfo, contexts, spec)
            except Exception as err:
                logger.error(
                    f"多版本订阅：规格「{spec.get('name')}」处理失败：{err} - {traceback.format_exc()}"
                )
        return count

    def __download_spec(self, subscribe, mediainfo, contexts: List[Any], spec: dict) -> int:
        """
        按单个规格筛选并下载
        """
        spec_name = spec.get("name")
        params = spec.get("params") or {}
        if not params:
            logger.warn(f"多版本订阅：规格「{spec_name}」没有任何过滤条件，已跳过")
            return 0

        handler = TorrentHelper()
        candidates = []
        for context in contexts:
            try:
                if handler.filter_torrent(context.torrent_info, params):
                    candidates.append(context)
            except Exception:
                continue
        if not candidates:
            logger.info(f"多版本订阅：「{subscribe.name}」规格「{spec_name}」没有匹配的资源")
            return 0

        progress = self.get_data("progress") or {}
        sub_key = str(subscribe.id)
        spec_state = progress.setdefault(sub_key, {}).setdefault(
            spec_name, {"torrents": [], "episodes": []}
        )
        done_torrents = set(spec_state.get("torrents") or [])
        done_episodes = {int(item) for item in (spec_state.get("episodes") or []) if str(item).isdigit()}

        # 同一个订阅下，同一种子只下载一次（不同规格可能同时命中同一个种子）
        all_downloaded = self.get_data("downloaded") or {}
        sub_downloaded = set(all_downloaded.get(sub_key) or [])

        is_tv = mediainfo.type == MediaType.TV
        target_episodes = self.__target_episodes(subscribe, mediainfo) if is_tv else None

        # 电影：同一规格已经下载过就不再重复下载（除非开启了「下载所有匹配资源」）
        if not is_tv and not self._download_all and done_torrents:
            logger.info(f"多版本订阅：「{subscribe.name}」规格「{spec_name}」已下载过，跳过")
            return 0

        count = 0
        for context in candidates:
            torrent = context.torrent_info
            torrent_key = self.__torrent_key(torrent)
            if torrent_key in done_torrents or torrent_key in sub_downloaded:
                continue

            episodes: Optional[Set[int]] = None
            if is_tv:
                # 只处理订阅对应季的资源
                torrent_seasons = set(context.meta_info.season_list or [])
                if torrent_seasons and subscribe.season and subscribe.season not in torrent_seasons:
                    continue
                torrent_episodes = {int(item) for item in (context.meta_info.episode_list or [])}
                if torrent_episodes:
                    # 单集/多集资源：与订阅的集数范围取交集，范围外的直接跳过
                    scope = torrent_episodes & target_episodes if target_episodes else torrent_episodes
                    if not scope:
                        logger.info(
                            f"多版本订阅：「{subscribe.name}」规格「{spec_name}」的候选"
                            f"「{torrent.title}」集数 {sorted(torrent_episodes)} 不在订阅集数范围内，跳过"
                        )
                        continue
                else:
                    # 整季/合集资源：按订阅范围内仍缺失的集处理
                    scope = target_episodes
                need = scope - done_episodes if scope else None
                if need is not None and not need:
                    continue
                episodes = need or None

            hash_str, error = DownloadChain().download_single(
                context=context,
                episodes=episodes,
                save_path=self._save_path or None,
                downloader=self._downloader or None,
                username="多版本订阅",
                source=f"MultiVersionSubscribe|{subscribe.id}|{spec_name}",
                return_detail=True
            )
            if not hash_str:
                logger.warn(f"多版本订阅：{torrent.title} 添加下载失败：{error}")
                continue

            count += 1
            done_torrents.add(torrent_key)
            sub_downloaded.add(torrent_key)
            if is_tv and episodes:
                done_episodes |= set(episodes)
            self.__add_history(subscribe, spec_name, context, hash_str, episodes)
            logger.info(f"多版本订阅：已添加下载「{torrent.title}」（规格：{spec_name}）")

            if not self._download_all:
                # 单规格只下一个：电影下一个即可
                if not is_tv:
                    break
                # 电视剧继续补齐仍缺失的集；无法确定剩余集数时只下一个
                if not target_episodes or not (target_episodes - done_episodes):
                    break

        if count == 0 and candidates:
            logger.info(f"多版本订阅：「{subscribe.name}」规格「{spec_name}」的候选资源都已下载过，跳过")

        spec_state["torrents"] = sorted(done_torrents)
        spec_state["episodes"] = sorted(done_episodes)
        all_downloaded[sub_key] = sorted(sub_downloaded)
        self.save_data("progress", progress)
        self.save_data("downloaded", all_downloaded)
        return count

    def __auto_delete(self):
        """
        下载完成后自动删除任务及文件
        """
        history = self.get_data("history") or []
        pending = [item for item in history if item.get("state") == "downloaded" and item.get("hash")]
        if not pending:
            return
        torrents = DownloadChain().list_torrents(
            status=TorrentStatus.TRANSFER,
            downloader=self._downloader or None
        ) or []
        finished = {torrent.hash for torrent in torrents if torrent.hash}
        if not finished:
            return
        changed = False
        for item in pending:
            if item.get("hash") not in finished:
                continue
            ok = DownloadChain().remove_torrents(
                hashs=[item["hash"]],
                delete_file=True,
                downloader=self._downloader or None
            )
            if ok:
                item["state"] = "deleted"
                item["deleted_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
                changed = True
                logger.info(f"多版本订阅：已删除任务及文件「{item.get('title')}」")
        if changed:
            self.save_data("history", history)

    # ------------------------------------------------------------------ #
    # 工具方法
    # ------------------------------------------------------------------ #
    def __update_config(self):
        config = {
            "enabled": self._enabled,
            "subscribes": self._subscribes,
            "spec_presets": self._spec_presets,
            "spec_combos": self._combos_text,
            "specs": self._specs_text,
            "interval": self._interval,
            "notify": self._notify,
            "auto_pause": self._auto_pause,
            "auto_delete": self._auto_delete,
            "download_all": self._download_all,
            "save_path": self._save_path,
            "downloader": self._downloader,
            "onlyonce": False
        }
        for sid, specs in self._sub_specs.items():
            config[f"{SUB_SPEC_PREFIX}{sid}"] = specs
        self.update_config(config)

    def __record_result(self, level: str, text: str):
        """
        记录最近一次运行结果，供详情页展示
        """
        self.save_data("last_result", {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": level,
            "text": text
        })

    def __spec_library(self) -> Dict[str, dict]:
        """
        可用规格库：预设 + 组合规格 + 自定义规格
        """
        library: Dict[str, dict] = {}
        for key, name, params in SPEC_PRESETS:
            library[key] = {"name": name, "params": dict(params), "preset": key}
        for combo in self.__parse_combos(self._combos_text):
            library[combo["name"]] = combo
        for spec in self.__parse_specs(self._specs_text):
            name = spec.get("name")
            if name:
                library.setdefault(name, spec)
        return library

    @staticmethod
    def __resolve_specs(identities: List[str], library: Dict[str, dict]) -> List[dict]:
        """
        按配置顺序取出规格，名称去重
        """
        specs: List[dict] = []
        seen: Set[str] = set()
        for identity in identities or []:
            spec = library.get(str(identity))
            if not spec:
                continue
            name = spec.get("name")
            if not name or name in seen:
                continue
            seen.add(name)
            specs.append(spec)
        return specs

    def __specs_for(self, subscribe) -> List[dict]:
        """
        订阅生效的规格：优先使用订阅单独配置，否则用默认规格
        """
        identities = self._sub_specs.get(str(subscribe.id)) or self._spec_presets
        return self.__resolve_specs(identities, self.__spec_library())

    @staticmethod
    def __parse_combos(text: str) -> List[dict]:
        """
        解析组合规格：一行一个，用 + 连接多个预设，必须同时满足

            4K+DV+60FPS
            4K杜比全景 = 4k + atmos
        """
        combos: List[dict] = []
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            name = ""
            if "=" in line:
                name, line = line.split("=", 1)
                name = name.strip()
            keys: List[str] = []
            for token in re.split(f"[{re.escape(COMBO_SEPARATORS)}]", line):
                token = token.strip()
                if not token:
                    continue
                key = SPEC_PRESET_ALIAS.get(token.lower())
                if key and key not in keys:
                    keys.append(key)
            if not keys:
                continue
            params = MultiVersionSubscribe.__combine_params(keys)
            if not params:
                continue
            if not name:
                name = "+".join(
                    SPEC_PRESET_SHORT.get(key) or SPEC_PRESET_MAP[key][0] for key in keys
                )
            combos.append({"name": name, "params": params, "combo": keys})
        return combos

    @staticmethod
    def __combine_params(keys: List[str]) -> Dict[str, str]:
        """
        把多个预设的正则条件合并成「全部满足」（正则前瞻）的过滤参数
        """
        patterns: List[str] = []
        excludes: List[str] = []
        size: Optional[str] = None
        for key in keys:
            preset = SPEC_PRESET_MAP.get(key)
            if not preset:
                continue
            _, params = preset
            for field in ("include", "resolution", "quality", "effect"):
                value = params.get(field)
                if value:
                    patterns.append(value)
            if params.get("exclude"):
                excludes.append(params["exclude"])
            if params.get("size"):
                size = params["size"]
        if not patterns:
            return {}
        combined = {"include": "".join(f"(?=.*(?:{pattern}))" for pattern in patterns)}
        if excludes:
            combined["exclude"] = "|".join(excludes)
        if size:
            combined["size"] = size
        return combined

    def __interval_minutes(self) -> int:
        """
        生效的执行周期（分钟）
        """
        override = (self._interval or "system").strip()
        if override.isdigit() and int(override) > 0:
            return int(override)
        return self.__system_interval_minutes()

    @staticmethod
    def __system_interval_minutes() -> int:
        """
        跟随系统：取「订阅搜索间隔」（小时）与「RSS 间隔」（分钟）中更短的一个
        """
        candidates: List[int] = []
        try:
            hours = int(getattr(settings, "SUBSCRIBE_SEARCH_INTERVAL", 0) or 0)
        except Exception:
            hours = 0
        if hours > 0:
            candidates.append(hours * 60)
        try:
            minutes = int(getattr(settings, "SUBSCRIBE_RSS_INTERVAL", 0) or 0)
        except Exception:
            minutes = 0
        if minutes > 0:
            candidates.append(max(minutes, 5))
        return min(candidates) if candidates else 30

    @staticmethod
    def __human_interval(minutes: int) -> str:
        """
        周期的人类可读文本
        """
        if minutes and minutes % 60 == 0:
            return f"{minutes // 60} 小时"
        return f"{minutes} 分钟"

    def __get_target_subscribes(self) -> List[Any]:
        try:
            all_subscribes = SubscribeOper().list()
        except Exception as err:
            logger.error(f"多版本订阅：读取订阅失败：{err}")
            return []
        if not self._subscribes:
            return []
        if "*" in self._subscribes:
            return list(all_subscribes)
        wanted = set(self._subscribes)
        return [subscribe for subscribe in all_subscribes if str(subscribe.id) in wanted]

    @staticmethod
    def __build_meta(subscribe):
        try:
            meta = MetaInfo(subscribe.name)
        except Exception:
            return None
        meta.year = subscribe.year
        meta.begin_season = subscribe.season
        try:
            meta.type = MediaType(subscribe.type)
        except Exception:
            meta.type = None
        meta.tmdbid = getattr(subscribe, "tmdbid", None)
        meta.doubanid = getattr(subscribe, "doubanid", None)
        meta.bangumiid = getattr(subscribe, "bangumiid", None)
        meta.anilistid = getattr(subscribe, "anilistid", None)
        meta.media_source = getattr(subscribe, "media_source", None)
        meta.media_id = getattr(subscribe, "media_id", None)
        meta.episode_group = getattr(subscribe, "episode_group", None)
        return meta

    @staticmethod
    def __get_sub_sites(subscribe) -> List[int]:
        """
        与原生订阅保持一致的站点范围计算
        """
        default_sites = SystemConfigOper().get(SystemConfigKey.RssSites) or []
        user_sites = list(subscribe.sites or [])
        if not user_sites:
            return default_sites
        if not default_sites:
            return user_sites
        intersection = [site for site in user_sites if site in default_sites]
        return intersection or default_sites

    @staticmethod
    def __base_filter_params(subscribe) -> Optional[dict]:
        """
        订阅自身的过滤参数（不含画质规格，画质规格由插件提供）
        """
        params: Dict[str, Any] = {}
        if subscribe.include:
            params["include"] = subscribe.include
        if subscribe.exclude:
            params["exclude"] = subscribe.exclude
        try:
            default_rule = SystemConfigOper().get(SystemConfigKey.SubscribeDefaultParams) or {}
        except Exception:
            default_rule = {}
        for key in ("tv_size", "movie_size", "min_seeders", "min_seeders_time"):
            if default_rule.get(key):
                params[key] = default_rule.get(key)
        return params or None

    @staticmethod
    def __target_episodes(subscribe, mediainfo) -> Optional[Set[int]]:
        """
        订阅需要下载的集数范围（开始集数 ~ 总集数）
        """
        season = getattr(subscribe, "season", None)
        season_episodes = ((getattr(mediainfo, "seasons", None) or {}).get(season) or [])
        total = subscribe.total_episode or (len(season_episodes) if season_episodes else 0)
        if not total:
            return None
        start = subscribe.start_episode or (min(season_episodes) if season_episodes else 1)
        if start > total:
            return None
        return set(range(int(start), int(total) + 1))

    @staticmethod
    def __torrent_key(torrent) -> str:
        return f"{torrent.site_name or ''}|{torrent.title or ''}"

    def __add_history(self, subscribe, spec_name, context, hash_str, episodes):
        history = self.get_data("history") or []
        title = f"{subscribe.name}（{subscribe.year}）" if subscribe.year else subscribe.name
        history.append({
            "time": time.strftime("%Y-%m-%d %H:%M:%S"),
            "subscribe": title,
            "subscribe_id": subscribe.id,
            "spec": spec_name,
            "title": context.torrent_info.title,
            "site": context.torrent_info.site_name,
            "size": context.torrent_info.size,
            "hash": hash_str,
            "episodes": sorted(episodes) if episodes else None,
            "state": "downloaded"
        })
        self.save_data("history", history[-500:])

    @staticmethod
    def __parse_specs(text: str) -> List[dict]:
        """
        解析画质规格文本

        推荐格式（key=value，用 | 分隔，第一个不带 = 的字段为规格名称）：
            1080p | resolution=1080[pi] | quality=BluRay
            4K | resolution=2160p | size=>8

        也兼容位置写法：名称|分辨率|质量|特效|包含|排除|大小(GB)

        支持的键：name / resolution / quality / effect / include / exclude / size
        同一字段内多个关键词用英文逗号分隔，会转换为正则或关系。
        """
        specs: List[dict] = []
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [part.strip() for part in line.split("|")]
            if any("=" in part for part in parts if part):
                # key=value 形式
                spec: Dict[str, str] = {}
                for part in parts:
                    if not part:
                        continue
                    if "=" in part:
                        key, value = part.split("=", 1)
                        key = key.strip().lower()
                        if key in SPEC_FIELDS:
                            spec[key] = value.strip()
                    elif not spec.get("name"):
                        spec["name"] = part
            else:
                # 位置形式
                values = parts + [""] * (len(SPEC_FIELDS) - len(parts))
                spec = dict(zip(SPEC_FIELDS, values[:len(SPEC_FIELDS)]))
            name = spec.get("name")
            if not name:
                continue
            params: Dict[str, str] = {}
            for key in ("resolution", "quality", "effect"):
                value = spec.get(key)
                if value:
                    keywords = [item.strip() for item in value.split(",") if item.strip()]
                    if keywords:
                        params[key] = "|".join(keywords)
            for key in ("include", "exclude"):
                if spec.get(key):
                    params[key] = spec[key]
            size = spec.get("size")
            if size:
                converted = MultiVersionSubscribe.__convert_size(size)
                if converted:
                    params["size"] = converted
            spec["params"] = params
            specs.append(spec)
        return specs

    @staticmethod
    def __convert_size(size: str) -> Optional[str]:
        """
        将 GB 为单位的大小条件转换为过滤模块使用的 MB 条件
        """
        def to_mb(value: str) -> Optional[str]:
            try:
                return str(int(float(value.strip()) * 1024))
            except Exception:
                return None

        size = size.strip()
        try:
            if "-" in size:
                left, right = size.split("-", 1)
                left_mb, right_mb = to_mb(left), to_mb(right)
                return f"{left_mb}-{right_mb}" if left_mb and right_mb else None
            if size.startswith((">", "<")):
                value_mb = to_mb(size[1:])
                return f"{size[0]}{value_mb}" if value_mb else None
            value_mb = to_mb(size)
            return f">{value_mb}" if value_mb else None
        except Exception:
            return None
