# Release workflow

The user keeps exactly two desktop editions: the original first release and one rolling test edition.

- Preserve `main`, the `v1.0.0` release, and the first edition's executable and installer.
- Continue development on `frontend/simulation-workspace`, which is now the rolling test branch. Do not create separate preview, equipment, or bug-fix editions.
- After appropriate verification, replace the test executable at `D:\Work\SimPy\SimPy Lab Studio 测试版\SimPy Lab Studio.exe`. Keep only this executable in that delivery folder. Stage and self-test builds in ignored repository output directories before replacing the delivered file.
- Replace `source-packages/SimPy-Lab-Studio-test-source.zip` and its checksum with the current test source snapshot; update the corresponding local source package under `D:\Work\SimPy\SimPy Lab Studio 源码包\测试版`. Retain first-edition source packages.
- Never remove user experiment data, API profile metadata, original research files, or the source repositories when cleaning superseded desktop deliverables. Confirm resolved paths and exact contents before deleting old generated output.
- Update the existing test branch and current deliverables in place on subsequent iterations. Git commit history can retain previous source revisions; it is not a collection of separately distributed software editions.

The simulation engine, metric calculations and physical model constraints remain outside a page-only change. AI adapter fixes already present in this branch must be retained.
