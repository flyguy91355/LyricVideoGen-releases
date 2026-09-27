#!/bin/bash
# Cuts a new release on the LyricVideoGen-releases distribution repo.
# Usage: scripts/cut_release.sh <version-tag> <notes-file>
# Example: scripts/cut_release.sh v1.1.0 /tmp/release-notes.txt
#
# Adapted from AITrading's scripts/cut_release.sh: same "always push a
# fresh snapshot before tagging" fix (a stale releases repo must never be
# tagged as if it were current), no severity argument (LyricVideoGen has
# no critical/routine distinction -- see
# docs/superpowers/specs/2026-09-08-update-available-design.md), and no
# config-file lookup for the repo name (hardcoded here and in
# lyricvideo/update/release_client.py's RELEASES_REPO constant).

set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <version-tag> <notes-file>"
    exit 1
fi

VERSION_TAG="$1"
NOTES_FILE="$2"
RELEASES_REPO="flyguy91355/LyricVideoGen-releases"

if [ ! -f "$NOTES_FILE" ]; then
    echo "Notes file not found: $NOTES_FILE"
    exit 1
fi

echo "Syncing a fresh code snapshot to $RELEASES_REPO before tagging..."
CLONE_DIR=$(mktemp -d)
trap 'rm -rf "$CLONE_DIR"' EXIT

git clone --quiet "https://github.com/${RELEASES_REPO}.git" "$CLONE_DIR"
find "$CLONE_DIR" -mindepth 1 -maxdepth 1 -not -name '.git' -exec rm -rf {} +

FILELIST=$(mktemp)
# Both launchers: apply.py's allow-list accepts a bare top-level .sh AND .bat,
# but until 2026-09-14 only the Linux one was ever synced, so a Windows install
# could never receive a launcher fix through Apply Update.
# deep_review/ and scripts/ (2026-09-26): tests/deep_review/ always shipped but
# the deep_review package it imports did not (pytest could not even collect it),
# and the scripts CLAUDE.md and the app's own log messages tell the owner to run
# never reached an Apply-Update install. Keep this list and apply.py's
# ALLOWED_PATH_PREFIXES in step (tests/test_update_manifest.py checks it).
git ls-files -- lyricvideo/ deep_review/ scripts/ tests/ docs/ requirements.txt CLAUDE.md \
    run_playalongvideoproduction.sh run_playalongvideoproduction.bat > "$FILELIST"
while IFS= read -r f; do
    mkdir -p "$CLONE_DIR/$(dirname "$f")"
    # git show HEAD:"$f", NOT `cp "$f" ...` -- cp would copy the WORKING TREE
    # file, silently sweeping any uncommitted local change into a public
    # numbered release. git ls-files only lists tracked PATH NAMES; it says
    # nothing about whether the working copy matches what's committed.
    # (Confirmed real: 2026-09-08's v1.0.1 was cut with `cp` while unrelated
    # uncommitted work was sitting in the tree, and the public release ended
    # up containing that unreviewed code.)
    git show "HEAD:$f" > "$CLONE_DIR/$f"
    # A shell redirect always creates the new file under the default umask
    # (typically 644) -- it never carries over git's own tracked executable
    # bit. Every release cut before this fix silently shipped
    # run_playalongvideoproduction.sh as non-executable (confirmed live,
    # 2026-09-10: Apply Update kept re-breaking the desktop launcher every
    # single time, even after manually chmod +x'ing it locally, because the
    # *source* being copied from was already broken at the release-repo
    # level). Restore the tracked mode explicitly.
    mode=$(git ls-tree HEAD -- "$f" | awk '{print $1}')
    if [ "$mode" = "100755" ]; then
        chmod +x "$CLONE_DIR/$f"
    fi
done < "$FILELIST"
# Every path this release ships, one per line. lyricvideo/update/apply.py compares
# it with the manifest of the release applied before, and removes files a newer
# release no longer ships (a module or test deleted upstream otherwise stays in an
# Apply-Update install forever). Like RELEASE_SOURCE_COMMIT below, it is not in
# apply.py's ALLOWED_PATH_PREFIXES, so it is never itself copied into an install.
cp "$FILELIST" "$CLONE_DIR/RELEASE_MANIFEST"
rm -f "$FILELIST"

# Records which commit of THIS repo (not the releases repo) the snapshot
# above was built from. lyricvideo/update/apply.py reads this back before
# applying an update: if the owner's checkout already contains this commit
# in its own git history, the release is stale relative to local work and
# applying it would silently revert any local commits made since (real
# incident, 2026-09-11: v1.8.2 was cut from 4917284, a further local commit
# landed 41 minutes later, and Apply Update then overwrote that commit's
# doc changes with the older release content). Never itself copied into an
# install -- it isn't in apply.py's ALLOWED_PATH_PREFIXES.
git rev-parse HEAD > "$CLONE_DIR/RELEASE_SOURCE_COMMIT"

pushd "$CLONE_DIR" > /dev/null
git add -A
if git diff --cached --quiet; then
    echo "No code changes since the last release sync -- releases repo already current."
else
    git -c user.email="flyguy91355@gmail.com" -c user.name="flyguy91355" \
        commit --quiet -m "Sync $(date -u +%Y-%m-%d) for $VERSION_TAG"
    git push --quiet origin main
    echo "Pushed fresh code snapshot."
fi
popd > /dev/null

gh release create "$VERSION_TAG" \
    --repo "$RELEASES_REPO" \
    --title "$VERSION_TAG" \
    --notes-file "$NOTES_FILE"

echo "Released $VERSION_TAG to $RELEASES_REPO"
