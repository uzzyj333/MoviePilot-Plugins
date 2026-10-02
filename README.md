# MoviePilot 插件：多版本订阅

MoviePilot **V2** 插件，解决「原生订阅只能留一个版本」的问题。

同一个剧，可以同时保留 **1080p / 4K / 杜比视界** 等多个版本：
每个规格独立搜索、独立下载、独立记录进度，**无关出种顺序**，
并且不依赖媒体库状态，所以「下载后即删除、不入库、不刮削」的场景同样可用。

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
   - 勾选「启用插件」；
   - 勾选「接管订阅（暂停原生搜索）」；
   - 选中要接管的订阅；
   - 填写画质规格，例如：

     ```
     1080p | resolution=1080[pi] | quality=BluRay
     4K | resolution=2160p | size=>8
     杜比视界 | resolution=2160p | effect=Dolby[\s.]+Vision,杜比视界
     ```

5. 保存（可勾「保存后立即运行一次」验证）。

详细说明见 [plugins.v2/multiversionsubscribe/README.md](plugins.v2/multiversionsubscribe/README.md)。

## 发布为自定义插件市场

1. 把本仓库推到 GitHub（或任意可访问的 Git 仓库）。
2. 在 MoviePilot「设置 → 插件市场」中把仓库地址加入市场源。
3. 保持 `package.v2.json` 里的 `version` 与 `__init__.py` 里的 `plugin_version` 一致。

> 提示：`plugin_icon` 使用的是插件市场 `icons/` 目录里的现成图标 `torrent.png`。
> 想换成自己的图标，把图片放进仓库的 `icons/` 目录，并同步修改
> `package.v2.json` 的 `icon` 与 `__init__.py` 的 `plugin_icon`。
