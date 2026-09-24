# Releasing

> [!CAUTION]
> **NEVER PUBLISH A GITHUB RELEASE FOR A SHORTENED VERSION TAG SUCH AS `v1`, `v2`, `v1.4`, OR `v2.3`.**
>
> These are movable aliases. Publishing an immutable release for one would permanently prevent that tag from being moved or reused, even if the release is deleted.
>
> GitHub releases must use a complete version tag matching `vMAJOR.MINOR.PATCH`, for example:
>
> - `v1.4.3`
> - `v2.0.0`
> - `v12.6.1`

## Release process

1. Confirm the intended commit is on `main` and all checks pass.
2. Draft a GitHub release using a new `vMAJOR.MINOR.PATCH` tag.
3. Verify the tag, target commit, and release notes.
4. Publish the release. The complete version tag will become immutable.
5. Move the corresponding major-version tag to the released commit.

   ```bash
   VERSION=v1.4.3
   MAJOR="${VERSION%%.*}" # v1.4.3 -> v1

   git fetch --tags
   git tag --force "$MAJOR" "${VERSION}^{}"
   git push origin "refs/tags/$MAJOR" --force
   ```

6. Verify that both tags resolve to the same commit.

   ```bash
   git fetch --tags --force
   git rev-parse "$MAJOR"
   git rev-parse "${VERSION}^{}"
   ```

Do not create releases retroactively for existing tags. Start with the next version.
