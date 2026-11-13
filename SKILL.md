---
name: douyin-auto-upload
description: |
  抖音剪映切片批量上传技能。把本地视频逐条上传到抖音创作者中心，自动填写标题/简介/话题、封面、商品链接与商品短标题，并停在发布前由用户本人点发布。
  触发词：上传抖音视频、剪映切片批量上传、上传目录里的视频、继续上传下一条、查看上传进度、登录抖音后台、扫码登录、检查登录、重新登录抖音。
  不负责自动点发布，不代替用户扫码；扫码后在等待命令中自动检测登录完成并抓取账号态。
---

# 抖音自动上传 Skill v1.0.1

## 硬性约束

- 永不自动点「发布」；只能停在发布前，由用户本人点。
- 登录只能用户本人扫码；agent 不代扫。
- 扫码后不需要用户再说“登录好了”：只要 agent 正在运行 account check --wait-login，skill 会自动轮询 READY、抓取账号态；只有超时/二维码过期才再问用户。
- 一次只允许一个上传任务。
- 不删除、不移动用户原视频。
- 带链接视频必须有商品链接/商品ID；普通视频不得自动添加商品。
- 普通视频的标题/简介/话题必须用户确认后才能上传。
- 失败默认整批暂停并报告，不自动跳过、不反复重试。
- 只支持 Windows / macOS，v1.0 不支持 Linux。

## 初始化

首次先确定数据根（skill 外部，禁止把用户数据放进 skill 包）：

~~~bash
python scripts/cli.py init --data-root "<用户数据根>"
~~~

建议非系统盘目录；数据根下会生成 config.json、data/、runtime/、secrets/。

init 会在 skill 目录写 config.local.json 作为数据根指针；这个文件不要随 skill 分发。

## 环境自检

~~~bash
python scripts/cli.py doctor
~~~

检查 Python、Chrome、websocket-client、数据根、账号/内容包数量。

## 账号

新建账号（新 profile，扫码登录）：

~~~bash
python scripts/cli.py account add --display "示例账号" --slug example-account
python scripts/cli.py account check --slug example-account --wait-login 300
~~~

使用已有 profile：

~~~bash
python scripts/cli.py account add --display "示例账号" --slug example-account \
  --profile-mode existing --profile-path "D:\...\profiles\example-account" --port 9222
~~~

账号必须 READY。未登录时：

1. 打开账号 Chrome，让用户本人扫码；
2. 运行 account check --slug ... --wait-login 300；
3. skill 轮询登录状态，READY 后自动抓取账号态并更新 registry。

## 登录态抓取和恢复

account check READY 后会抓取并保存：

- cookies；
- localStorage；
- sessionStorage；
- IndexedDB 元数据/记录（尽力）；
- cookie 指纹、抖音号、昵称、抓取时间。

抓取文件在数据根 secrets/，属于敏感数据，不随 skill 分发。

说明：这是“登录所需的核心账号态”，不是浏览器全量镜像：

- IndexedDB 的 Blob/ArrayBuffer 只保存类型/大小/前 2048 字节摘要，不保存完整二进制；
- Cache Storage / Service Worker 缓存不抓取；
- 恢复时重建 cookies + localStorage + sessionStorage，不重建 IndexedDB 数据库；
- Chrome profile 本身仍是主登录态，快照是失效时的兜底。

profile 登录失效时，account check 会先用 cookie 快照尝试恢复；恢复失败再让用户扫码。恢复成功不会自动发布，仍需走上传前停在发布前的流程。

## 标签页 / TabGuard

沿用 30 号项目修过的方案：

- 上传时不再新开标签再关旧标签；fresh_page 复用当前页，about:blank → UPLOAD_URL 硬重置；
- 多余页面在 fresh_page 时关闭；
- 上传过程中启动 TabGuard，监听浏览器 Target 事件，关闭非 creator 弹窗/会话恢复页，日志写 runtime/logs/tabs/<job>.jsonl；
- 启动参数禁用 SessionRestore/崩溃气泡；
- 原生「要恢复页面吗」气泡在 Windows 下自动关闭；
- account check 从草稿页不导航，避免标签页刷新/关闭抖动。

TabGuard 只在上传过程到发布前之间运行；到发布前后停止，避免干扰用户点发布。

## 多账号

每个账号独立：

- 独立 Chrome profile；
- 独立 CDP 端口；
- 独立 cookie/账号态；
- 独立 registry 记录。

添加多个账号：

~~~bash
python scripts/cli.py account add --display "另一个账号" --slug another-account
python scripts/cli.py account list
~~~

上传时账号按内容包的 account_slug 自动选择；内容包没绑账号时用 --account 指定。一个批次可以跨账号，但一次只上传一条，账号浏览器可以同时打开。

账号登录失效、重复扫码、profile 串号时：

~~~bash
python scripts/cli.py account check --slug <slug> --wait-login 300
~~~

如果页面卡死/浏览器无响应，account check 会自动关闭并重启该账号浏览器后再检查；不会影响其他账号。

## 内容包

带链接视频的内容包：

~~~bash
python scripts/cli.py package add \
  --style-code 123456 \
  --name "[123456] 示例商品" \
  --product-name "商品完整名" \
  --product-link "https://haohuo..." \
  --product-id "385..." \
  --short-title "显瘦鹅绒服" \
  --title "标题（不超过30字）" \
  --desc "简介（不超过60字）" \
  --tags "穿搭,女装,羽绒服,小个子穿搭,显瘦穿搭" \
  --account example-account
~~~

普通视频内容包：

~~~bash
python scripts/cli.py package add --plain \
  --style-code 123456 \
  --name "[普通] 123456 视频" \
  --short-title "显瘦鹅绒服" \
  --title "用户确认过的标题" \
  --desc "用户确认过的简介" \
  --tags "穿搭,女装,羽绒服,小个子穿搭,显瘦穿搭" \
  --account example-account
~~~

普通视频不添加商品卡、不添加商品短标题；保存权限保持页面默认；封面仍自动选推荐封面。

## 扫描和计划

~~~bash
python scripts/cli.py scan --dir "D:\剪映导出视频保存"
python scripts/cli.py plan --dir "D:\剪映导出视频保存" --write
~~~

扫描会读 mp4/mov、计算 SHA256、解析款号、匹配商品/内容包、判断 linked/plain_candidate、报告重复/缺内容/缺账号。

auto 模式匹配不到内容包时不会悄悄降级为普通视频，会要求先补内容包或明确用 --mode plain。

## 上传

~~~bash
# 默认带链接，semi_auto，10 分钟未检测到发布就暂停
python scripts/cli.py upload --dir "D:\剪映导出视频保存" --mode auto --wait-login 300

# 普通视频批次
python scripts/cli.py upload --dir "D:\剪映导出视频保存" --mode plain --continue-mode semi_auto

# 手动模式
python scripts/cli.py upload --dir "..." --continue-mode manual
python scripts/cli.py continue
~~~

每条流程：

1. 校验视频；
2. 确保账号 READY；
3. 新文档打开上传页；
4. 设置文件并等待上传启动（20 秒看门狗）；
5. 填标题/简介/话题；
6. linked：保存权限不允许 → 封面 → 商品链接 → 商品短标题；
7. plain：只选推荐封面，不动保存权限/商品/扩展信息；
8. 停在发布前，输出检查点路径；
9. 按 continue-mode 等待/继续。

## 继续策略

- manual：停在发布前，用户发布后运行 continue。
- semi_auto（默认）：自动等待；默认 10 分钟没检测到发布就暂停。
- auto：自动等待更久（内部 60 分钟），检测到发布后继续下一条。

## 状态

~~~bash
python scripts/cli.py status
python scripts/cli.py pause
python scripts/cli.py resume
~~~

状态保存在数据根 runtime/batches/current.json。

## 失败处理

- 登录掉线：批次状态变为 login_required，提示用户扫码；运行 account check --wait-login 300 后 continue；
- 上传开始时检测到未登录：用 upload --wait-login 300，skill 会等待扫码并自动抓取账号态后继续；
- 未发布草稿：account check 检测到 content/post 或 content/upload 时不导航、不自动恢复；upload 检测到草稿页会直接拒绝下一条，避免 beforeunload「确定离开网页」卡死或覆盖草稿；
- 必须离开草稿页时：CDP 只对 beforeunload 自动确认；其他弹窗不替用户点；手动刷新/关闭仍由 Chrome 自己提示；
- 页面卡死/浏览器无响应：account check 会自动重启该账号浏览器（会丢失未保存草稿，运行前注意）；
- 商品限额/无效：暂停，报告平台原文；
- 上传启动停滞：报 UPLOAD_START_STALL + 截图；
- 页面结构变化：暂停，保留截图，不反复重试；
- 普通视频：不操作「视频章节/位置/热点/共创/合集/自主声明」等扩展信息，留给 v2.0。

## 数据安全

- skill 包内不得放 config.local.json、账号、profile、cookie、内容包、商品链接、历史任务；
- 分享前只打包 SKILL.md/scripts/engine/references/requirements.txt/VERSION；
- 用户数据根与 skill 目录分离。
