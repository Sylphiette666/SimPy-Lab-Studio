# Release workflow

The user keeps the original first release and one rolling working edition. The user explicitly promoted the current tested application to `main` and requested numbered public releases. The current stable release is v1.2.0.

- The user corrected the publishing destination on 2026-09-17: use `https://github.com/Sylphiette666/SimPy-Lab-Studio`, remote `origin`, branch `roxy_beta`, and a prerelease named/tagged `roxy_beta`. This supersedes earlier requests to push to R-0xy/main. Remote `r0xy` retains the earlier uploaded copy. Do not change the destination repository's main branch or existing numbered releases for this beta task.
- `main` contains the current working update on the v1.2.0 desktop baseline. A push to main does not itself create a numbered release. Preserve historical tags, release archives and first-edition executables/installers.
- After appropriate verification, replace the test executable at `F:\Simpy\SimPy Lab Studio 测试版\SimPy Lab Studio.exe` (the current machine's workspace; the former `D:\Work\SimPy` path is unavailable). Keep only this executable in that delivery folder. Stage and self-test builds in ignored repository output directories before replacing the delivered file.
- Replace `source-packages/SimPy-Lab-Studio-test-source.zip` and its checksum with the current test source snapshot; update the corresponding local source package under `F:\Simpy\SimPy Lab Studio 源码包\测试版`. Retain first-edition source packages.
- Never remove user experiment data, API profile metadata, original research files, or the source repositories when cleaning superseded desktop deliverables. Confirm resolved paths and exact contents before deleting old generated output.
- Update `roxy_beta` and current test deliverables for this beta task. Git commit history retains previous source revisions, without separately distributed editions per feature.
- For explicitly requested stable releases, merge tested changes into `main`, synchronize the desktop/UI/EXE/installer version, verify installation and uninstall behavior, and publish a numbered GitHub Release. Keep prior release assets unchanged. Stable source archives use versioned filenames under `source-packages/`.

Preserve engine and physical model constraints unless the selected task requires changes. Supplementary diagnostics must identify their statistical method and distinguish sampled previews from full replication results.

Implement only selected improvement items. The current authorized batch comprises high-priority items 5, 9, 10, 13, 15, 17, 21, 24 and 25; repeated testing and corrections are authorized. Other roadmap items remain unselected.

The original numbered list and completed test-branch items are recorded in `docs/improvement-list.md`. Preserve its numbering when resolving follow-up requests.
