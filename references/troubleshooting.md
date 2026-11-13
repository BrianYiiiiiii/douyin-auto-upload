# 排障 v1.0

## Chrome 启动失败
- doctor 检查 chrome_path；
- 关闭同 profile 残留 Chrome 后重试；
- macOS 确认 Chrome.app 存在。

## 账号未 READY
- 打开账号浏览器让用户扫码；
- 运行 account check --slug。

## 商品限额
- 抖音提示「无法添加购物车」：账号限额，暂停，不反复重试。

## 上传启动停滞
- 20 秒内无进度/重新上传/编辑封面信号：暂停，保留检查点截图；
- 重新运行会使用新文档上传。

## 普通视频不要动扩展信息
- v1.0 不处理章节/位置/热点/共创/合集/自主声明，避免误操作。


## 登录失效 / 重新扫码

1. 让账号 Chrome 停在登录页，用户本人扫码；
2. 运行 account check --slug <slug> --wait-login 300；
3. account check 会轮询 READY，成功后自动抓取账号态并更新 registry；
4. 若之前有 cookie 快照，会先尝试恢复；恢复失败才等待扫码；
5. 上传过程中掉线：批次状态变 login_required，账号恢复后 continue。

## 页面卡死 / 浏览器无响应

- account check 遇到 CDP 超时会自动关闭并重启该账号浏览器；
- 会丢失该浏览器里未保存的草稿，操作前注意；
- 不要连续对卡死页面重试 CDP；先恢复浏览器再检查。

## 多账号

- 每个账号独立 profile、端口、secrets；
- account list 查看；
- 上传按内容包 account_slug 自动选账号，缺省时用 --account；
- 一次只上传一条，账号浏览器可同时打开。



## 未发布草稿 / 「确定离开网页」

- 原因：账号浏览器停在 content/post 或 content/upload 且有未发布草稿时，任何导航/刷新会触发 beforeunload。
- 处理：account check 检测到草稿 URL 不再导航回家；upload 检测到草稿页直接拒绝下一条。
- account check 从草稿页且未登录时也不会自动恢复 cookie（避免覆盖草稿），先让用户发布或关闭/放弃草稿。
- CDP 兜底只自动确认 beforeunload；普通 alert/confirm 不替用户点。
- 用户手动刷新/关闭草稿页时浏览器仍会弹确认，这是 Chrome 行为，agent 不要代替点击。
- 浏览器已经卡死时，account check 会重启该账号浏览器；会丢未保存草稿，已警告。



## Chrome「要恢复页面吗？」原生气泡

- 这是 Chrome 自己恢复上次崩溃/未正常关闭标签页的原生窗口，不是网页 DOM，页面 CDP 点不到。
- Windows 版 skill 启动/复用账号浏览器时会：
  1. 启动前把 profile Default/Preferences 的 exit_type 改成 Normal、exited_cleanly=true；
  2. 启动后用 Win32 枚举同 PID、标题为「要恢复页面」的窗口并关闭；
  3. 保留 --disable-session-crashed-bubble / --disable-features=SessionRestore 启动参数。
- macOS 暂未自动关闭这个原生气泡，需要用户点 X。
- 关闭气泡只放弃“恢复上次标签页”，不影响当前 profile 登录态；如果登录态已失效，用 account check 的 cookie 快照恢复或重新扫码。

