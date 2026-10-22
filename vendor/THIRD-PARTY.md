# 第三方组件

Arca 打包进 `Arca.app/Contents/Resources/app/vendor/` 的二进制与对应许可证：

| 文件 | 组件 | 版本 | 许可证 | 许可证文件 |
| --- | --- | --- | --- | --- |
| `7zz` | 7-Zip（Igor Pavlov） | 26.04 | LGPL-2.1+ / unRAR 限制条款 | `7-Zip-License.txt` |
| `zstd` | Zstandard 命令行（Meta） | 1.5.7 | 命令行工具为 GPL-2.0，库部分为 BSD-3-Clause | `GPL-2.0.txt`、`zstd-LICENSE.txt` |
| `par2` | par2cmdline（Peter Brian Clements 等） | 1.4.0 | GPL-2.0 | `GPL-2.0.txt` |

三个二进制均由本项目用 `scripts/build_thirdparty.py` 从上游 tag 源码直接编译为
`arm64 + x86_64` 通用二进制，未做代码修改（zstd 仅关闭 x86-64 汇编路径：
`-DZSTD_DISABLE_ASM`，因为该汇编符号在 universal 构建下无法解析）。

对应源码地址：

- https://github.com/ip7z/7zip （`7z2604-src.tar.xz`，官方预编译包内附）
- https://github.com/facebook/zstd tag `v1.5.7`
- https://github.com/Parchive/par2cmdline tag `v1.4.0`

GPL 组件的完整对应源码可从上述地址获取；如需与 Arca 一同分发修改版本，
请遵循 GPL-2.0 的源码提供义务。
