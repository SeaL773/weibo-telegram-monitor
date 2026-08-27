# Third-Party Notices

This project depends on third-party software. Those components remain governed by their own licenses; the project's MIT License does not replace them.

The direct Python dependencies declared in `requirements.txt` are summarized below. License names are based on upstream project metadata available when this notice was written:

| Component | Declared version | Upstream license | Project |
|---|---:|---|---|
| httpx | 0.28.1 | BSD 3-Clause | <https://github.com/encode/httpx> |
| pytz | 2026.1.post1 | MIT | <https://pythonhosted.org/pytz/> |
| Pyrogram | 2.0.106 | GNU LGPL v3 or later | <https://github.com/pyrogram/pyrogram> |
| TgCrypto | 1.2.5 | GNU LGPL v3 or later | <https://github.com/pyrogram/tgcrypto> |
| Playwright for Python | 1.58.0 | Apache License 2.0 | <https://github.com/microsoft/playwright-python> |
| Loguru | 0.7.3 | MIT | <https://github.com/Delgan/loguru> |

The Docker build also installs or contains additional software, including:

- the Python base image and Debian packages;
- Chromium and browser support installed by Playwright;
- FFmpeg and its codec libraries;
- transitive Python and operating-system dependencies.

FFmpeg's effective license can vary with build configuration and enabled codec libraries. Chromium and Debian packages include many separately licensed components. Before redistributing a built container image, generate a software bill of materials from that exact image, inspect its package license files, and verify all source-offer, attribution, notice, and redistribution obligations. Do not rely on this summary as a complete image-level notice.

Useful verification commands include:

```bash
python -m pip show httpx pytz pyrogram tgcrypto playwright loguru
docker image inspect IMAGE_NAME
docker run --rm IMAGE_NAME sh -lc 'cat /etc/os-release; dpkg-query -W'
```

For authoritative license text and exceptions, consult each installed package, the linked upstream project, and the exact container image being distributed. If upstream metadata differs from this file, verify the installed artifact and update this notice before release.
