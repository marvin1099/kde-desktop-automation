# Releasing

Codeberg is the canonical repository and `main` is its primary branch. GitHub
is a push mirror. Releases are prepared as drafts on both forges; review both
drafts and publish them with the same notes after checking their assets.

## One-time setup

- Enable Woodpecker CI for the Codeberg repository. Codeberg's hosted CI uses
  Woodpecker; the repository must be activated there before this pipeline runs.
- Add a Codeberg repository secret named `CODEBERG_RELEASE_TOKEN` with a token
  that can create/edit releases and upload assets. Keep it scoped to this
  repository and do not use it for ordinary test steps.
- Enable GitHub Actions on the mirror. The GitHub release workflow uses its
  built-in `GITHUB_TOKEN` with write permission only in the release job.
- The Codeberg `.woodpecker.yml` and GitHub Actions workflows run tests, lint,
  formatting, type checks, and a package build. Ordinary tests do not start a
  private KWin compositor; those integration tests remain opt-in.

## Prepare a release

1. Choose the version and update `project.version` in `pyproject.toml`, then run
   `uv lock` and commit the resulting lockfile change.
2. Run the checks in the README and make sure the worktree is clean and the
   commit is on `main`.
3. Create an annotated version tag that exactly matches the project version:

   ```bash
   git tag -a v0.1.0 -m "Release v0.1.0"
   python scripts/check_release_tag.py v0.1.0
   ```

4. Push the branch to Codeberg and the GitHub mirror. Push the same tag to
   Codeberg first; wait for its checks and draft release, then push the tag to
   GitHub:

   ```bash
   git push codeberg main
   git push github main
   git push codeberg v0.1.0
   git push github v0.1.0
   ```

5. Review the draft release on each forge. Confirm both use the same tag and
   source commit, inspect the wheel, source archive, and `SHA256SUMS`, add the
   same release notes, then publish both drafts.

The Codeberg draft is created by Woodpecker and the GitHub draft by GitHub
Actions. Neither workflow publishes a release automatically. The tag is rejected
by the release checks if it does not equal `v` plus the version in
`pyproject.toml`.
