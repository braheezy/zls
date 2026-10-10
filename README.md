<img src="https://raw.githubusercontent.com/zigtools/zls/master/.github/assets/zls-opt.svg" alt="ZLS Logo" width=200>

[![CI](https://github.com/zigtools/zls/actions/workflows/main.yml/badge.svg)](https://github.com/zigtools/zls/actions/workflows/main.yml)
[![codecov](https://codecov.io/github/zigtools/zls/graph/badge.svg?token=WE18MPF00W)](https://codecov.io/github/zigtools/zls)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](https://opensource.org/licenses/MIT)
[![Discord](https://discord.com/api/guilds/997174129532866701/widget.png?style=shield)](https://discord.gg/5m5U3qpUhk)

ZLS is a non-official implementation of the [Language Server Protocol](https://microsoft.github.io/language-server-protocol/) for [Zig](https://ziglang.org/) in Zig. It provides developers with IDE [features](#features) in their editor.

## Installation

The complete installation guide is available on the [Zigtools website](https://zigtools.org/zls/install/). It covers editor setup, prebuilt binaries and additional documentation.

### Build From Source

This fork supports stock Zig **0.17.0**, including build-system module resolution,
generated modules, `zls.build.json` build options, and build-on-save diagnostics.
The integration is based on upstream's `techatrix/build-runner-process` work
([#3208](https://github.com/zigtools/zls/issues/3208)); no compiler fork is required.

The default branch of ZLS targets [Zig master](https://ziglang.org/download/).

```bash
git clone https://github.com/zigtools/zls
cd zls
zig build -Doptimize=ReleaseSafe
```

When upgrading Zig, make sure to update ZLS to keep them in sync.

To use the local build in Zed, set an absolute path in your settings:

```json
{
  "lsp": {
    "zls": {
      "binary": {
        "path": "/Users/braheezy/code/zls/zig-out/bin/zls"
      }
    }
  }
}
```

Restart Zed's language server after rebuilding. Build-on-save automatically uses
`check` when available; `build_on_save_args` can select other steps and pass build
flags. These builds execute the project's build script and selected steps.
Build-on-save is driven by editor save notifications, not native filesystem
watching; external disk changes alone do not trigger it. Saving `build.zig`
restarts the build session so configuration errors can recover.

### Build-system regression tests

```sh
zig build test
python3 tests/build_system.py --zls /absolute/path/to/zig-out/bin/zls --zig /absolute/path/to/zig
```

The Python harness checks actual LSP module navigation, generated files, local
package dependencies, project options, compiler diagnostics, build-script error
recovery, and shutdown during a running build. It requires Python 3.9+ and no
third-party Python packages. Use `--cache-dir /path/to/zig/global-cache` to reuse
compiler caches across runs.

The integration is tied to Zig 0.17's build-protocol/configuration format. Stock
Zig does not report all installation-relative or libc-runtime base paths, so
modules using those unusual path bases may remain unresolved.

## Features

ZLS supports most language features, including simple type function support, payload capture type resolution, custom packages, and others. Support for comptime and semantic analysis is Work-in-Progress.

The majority of LSP features are supported:

- Completions
- Hover
- Diagnostics and opt-in [build-on-save](https://zigtools.org/zls/guides/build-on-save/)
- Go to definition/declaration
- Workspace symbols and document symbols
- Find references and rename symbol
- Formatting based on `zig fmt`
- Semantic tokens highlighting
- Inlay hints
- Code actions

## Quick Thanks :)

We'd like to take a second to thank all our awesome [contributors](https://github.com/zigtools/zls/graphs/contributors) and donators/backers/sponsors; if you have time or money to spare, consider partaking in either of these options - they help keep ZLS awesome for everyone!

[![OpenCollective Backers](https://opencollective.com/zigtools/backers.svg?width=890&limit=1000)](https://opencollective.com/zigtools#category-CONTRIBUTE)
