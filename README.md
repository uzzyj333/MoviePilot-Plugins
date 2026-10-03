# MoviePilot 插件：多版本订阅

MoviePilot **V2** 插件，解决「原生订阅只能留一个版本」的问题。

同一个剧，可以同时保留 **1080p / 4K / 杜比视界** 等多个版本：

- 画质规格在插件里**多选**（跟 MP 自带的过滤规则组一样，选几个就下几个版本）；
- **组合规格**（如 `4K+DV+60FPS`）表示条件**同时满足**，一个组合就是一个版本；
- **每个订阅可以有自己的规格**（A 剧 4K+DV，B 剧只留 1080p）；
- 每个规格独立搜索、独立下载、独立记录进度，**无关出种顺序**，也不受洗版优先级限制；
- 站点、过滤规则组、包含/排除、自定义识别词**全部沿用订阅自己的配置**，插件只负责画质规格；
- 电视剧严格按订阅的**开始集数 ~ 总集数**下载，范围之外的剧集不会下载；
- 执行周期可**跟随系统**（取系统「订阅搜索间隔」与「RSS 间隔」中更短的一个），也可直接选固定周期，不用填 cron；
- 不依赖媒体库状态，「下载后即删除、不入库、不刮削」的场景同样可用。

## 目录结构

```
.
├── package.v2.json                    # 插件市场索引（仓库根目录）
├── icons/
│   └── torrent.png                    # 插件图标
├── plugins.v2/
│   └── multiversionsubscribe/
│       ├── __init__.py                # 插件实现
│       └── README.md                  # 完整使用说明
└── README.md
```

本仓库就是一个可直接使用的 **MoviePilot V2 插件市场源**，
在 MoviePilot「设置 → 插件市场」里添加本仓库地址即可搜到安装。

## 快速安装

1. 把 `plugins.v2/multiversionsubscribe` 复制到 MoviePilot 插件目录：
   - Docker：`./config/plugins/multiversionsubscribe`
   - 源码：`app/plugins/multiversionsubscribe`
2. 重载插件（或重启 MoviePilot）。
3. 在 MoviePilot「订阅」页面建好订阅：**只填订阅站点和过滤规则组，质量/分辨率/特效留空**。
4. 在「插件 → 多版本订阅 → 设置」里：
   - 勾选「启用插件」与「接管订阅（暂停原生搜索）」；
   - 选中要接管的订阅；
   - **直接勾选默认画质规格**，例如「1080p」「4K」「杜比视界（Dolby Vision）」；
   - 需要「同时满足」的版本，在「组合规格」里写 `4K+DV+60FPS` 这样的组合；
   - 想让某个剧用自己的规格：保存一次后重新打开本页，在该剧的「单独规格」里勾选（留空 = 用默认规格）；
   - 保存（可勾「保存后立即运行一次」验证）。
5. 详情页会显示「最近运行结果」，可以点「立即运行」随时手动跑一次。

详细说明见 [plugins.v2/multiversionsubscribe/README.md](plugins.v2/multiversionsubscribe/README.md)。

## 发布为自定义插件市场

1. 把本仓库推到 GitHub（或任意可访问的 Git 仓库）。
2. 在 MoviePilot「设置 → 插件市场」中把仓库地址加入市场源。
3. 保持 `package.v2.json` 里的 `version` 与 `__init__.py` 里的 `plugin_version` 一致。

> 提示：`plugin_icon` 使用的是本仓库 `icons/` 目录里的 `torrent.png`。
> 想换成自己的图标，把图片放进 `icons/` 目录，并同步修改
> `package.v2.json` 的 `icon` 与 `__init__.py` 的 `plugin_icon`。
