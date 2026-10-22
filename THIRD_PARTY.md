# Components and provenance

This project's Python pipeline and generic static UI were refactored from an earlier private deployment. The public repository starts with a new history and synthetic fixtures. Operational source paths, account identifiers, real recordings, mined vocabulary, deployment evidence and personal artwork are not distributed.

External components are independently maintained and keep their own licenses:

- [BililiveRecorder](https://github.com/BililiveRecorder/BililiveRecorder): optional upstream container, pinned to release 2.20.0 in examples. It is not copied into this repository.
- [BaiduPCS-Go](https://github.com/qjfoidnh/BaiduPCS-Go): v4.0.1 binary fetched from its official release during image build, verified against release SHA-256. Its [upstream license](https://github.com/qjfoidnh/BaiduPCS-Go/blob/master/LICENSE) applies to that binary.
- [FFmpeg](https://ffmpeg.org/legal.html): installed from Debian packages; package build options determine applicable licensing.
- [Python](https://docs.python.org/3/license.html), Debian base packages and the packages in requirements.lock retain their upstream notices.
- The compose.sh fallback uses the official Docker CLI image; Docker and Compose retain their own licenses.

If distributing built images, preserve the binaries' notices and comply with their license/source requirements. The MIT license in this repository covers this project's code, not third-party components or recorded content.
