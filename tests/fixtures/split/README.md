分卷测试夹具不再提交进 git（7z 内嵌时间戳导致重建字节漂移）。
生成物在 build/fixtures/split/，由 scripts/split_fixtures.py 在
selftest 每次运行时自动重建并清理 AppleDouble `._*`。
