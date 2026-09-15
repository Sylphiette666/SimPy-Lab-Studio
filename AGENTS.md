# Release workflow

The user keeps the original first release and one rolling working edition. The user explicitly promoted the current tested application to `main` and requested numbered public releases. The current stable release is v1.2.0.

- `main` now contains the current stable release. Preserve the immutable `v1.0.0` tag/release and the first edition's executable and installer; do not keep `main` frozen at v1.0.0.
- Continue development on `frontend/simulation-workspace`, which is now the rolling test branch. Do not create separate preview, equipment, or bug-fix editions.
- After appropriate verification, replace the test executable at `D:\Work\SimPy\SimPy Lab Studio 测试版\SimPy Lab Studio.exe`. Keep only this executable in that delivery folder. Stage and self-test builds in ignored repository output directories before replacing the delivered file.
- Replace `source-packages/SimPy-Lab-Studio-test-source.zip` and its checksum with the current test source snapshot; update the corresponding local source package under `D:\Work\SimPy\SimPy Lab Studio 源码包\测试版`. Retain first-edition source packages.
- Never remove user experiment data, API profile metadata, original research files, or the source repositories when cleaning superseded desktop deliverables. Confirm resolved paths and exact contents before deleting old generated output.
- Update the existing test branch and current deliverables in place on subsequent iterations. Git commit history can retain previous source revisions; it is not a collection of separately distributed software editions.
- For explicitly requested stable releases, merge tested changes into `main`, synchronize the desktop/UI/EXE/installer version, verify installation and uninstall behavior, and publish a numbered GitHub Release. Keep prior release assets unchanged. Stable source archives use versioned filenames under `source-packages/`.

The simulation engine, metric calculations and physical model constraints remain outside a page-only change. AI adapter fixes already present in this branch must be retained.

The user selects numbered improvement items one at a time. Implement only the item explicitly requested; do not automatically continue through the improvement list.

The original numbered list and completed test-branch items are recorded in `docs/improvement-list.md`. Preserve its numbering when resolving follow-up requests.
