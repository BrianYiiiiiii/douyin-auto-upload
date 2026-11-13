# 抖音自动上传 Skill v1.0.1

独立 skill：把本地视频逐条上传到抖音创作者中心，自动填写标题/简介/话题、封面、商品链接与商品短标题，停在发布前由用户本人点发布。

## 前置条件

- Windows 10/11 或 macOS
- Python 3.11+
- Google Chrome
- 自己的抖音创作者账号
- 网络可安装 Python 依赖

不包含任何账号、profile、cookie、商品、内容包或历史数据。

## 从 GitHub 获取

~~~bash
git clone https://github.com/BrianYiiiiiii/douyin-auto-upload.git
~~~

也可以从仓库 Releases 页面下载 DouyinAutoUploadSkill_v1.0.0_*.zip。

## 安装

把本目录放到 DSH skill 目录，例如：

~~~text
<skills-root>/douyin-auto-upload
~~~

### Windows

~~~powershell
powershell -ExecutionPolicy Bypass -File .\setup.ps1 -DataRoot "<data-root>"
~~~

### macOS

~~~bash
chmod +x setup.sh
./setup.sh
~~~

setup 会：

1. 检查 Python 3.11；
2. 检查/安装 websocket-client；
3. 检查 Chrome；
4. 初始化数据根；
5. 运行 doctor。

数据根建议选非系统盘；账号和 profile 都在数据根下。

## 首次使用

~~~powershell
python scripts/cli.py account add --display "你的账号名" --slug your-account
python scripts/cli.py account check --slug your-account --wait-login 300
~~~

扫码登录后，skill 会自动检测 READY 并抓取 cookies/localStorage/sessionStorage/IndexedDB。

## 建内容包

带链接视频：

~~~powershell
python scripts/cli.py package add --style-code 123456 \
  --name "[123456] 商品名" \
  --product-name "商品完整名" \
  --product-link "https://haohuo..." \
  --product-id "商品ID" \
  --short-title "短标题" \
  --title "标题" \
  --desc "简介" \
  --tags "穿搭,女装,羽绒服,小个子穿搭,显瘦穿搭" \
  --account your-account
~~~

普通视频加 --plain，不需要商品链接。

## 扫描和上传

~~~powershell
python scripts/cli.py scan --dir "D:\视频目录"
python scripts/cli.py upload --dir "D:\视频目录" --mode auto --wait-login 300
~~~

默认 semi_auto：每条停在发布前，用户点发布后检测并继续下一条；10 分钟未发布暂停。

## 数据位置

~~~text
<数据根>\
  config.json
  data/accounts.json
  data/products.json
  data/packages/
  data/video_package_map.json
  data/uploaded_hashes.json
  runtime/profiles/
  runtime/logs/
  runtime/batches/
  secrets/
~~~

secrets/ 是敏感登录态，禁止分享。

## 已知限制

- Windows 已实测；macOS 代码已适配但未实机验证。
- 不自动点发布、不代替扫码。
- 必须自带 Chrome 和抖音账号。
- 不内置 Python/Chrome。
- 仅适用于用户自己有权操作的账号；请遵守抖音平台规则。

## 免责声明

本项目不是抖音官方产品，与抖音/字节跳动无关联。使用自动化操作存在账号限流、限权、封禁等风险，请自行评估并承担后果。不要用本项目发布违法、侵权或违反平台规则的内容。

## License

本项目使用 [MIT License](LICENSE)。

可以自由使用、修改、分发和商用，但需保留版权与许可声明；不提供任何担保。
