# 数据目录 v1.0

数据根由首次 init 选择，不在 skill 包内。

- data/accounts.json：账号 registry（兼容原 project 30 schema）
- data/products.json：商品登记（style_code/product_id/product_link/short_title/account_scope）
- data/packages/：内容包 JSON（复用 project 30 package schema）
- data/video_package_map.json：视频到内容包映射
- data/uploaded_hashes.json：已上传视频 SHA 记录（防重复）
- runtime/profiles/：新建账号的 Chrome profile
- runtime/logs/：日志
- runtime/batches/current.json：当前批次状态
- secrets/：账号态/cookie 快照，敏感，禁止分享
