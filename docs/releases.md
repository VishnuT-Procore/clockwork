# Versions and changes

What's live in each region, and what a release changes. Back to the [README](../README.md).

## What's in this release

Tell the train how to find the changes between the version it's deploying and the one
each platform runs now (see [Live versions](#live-versions-per-platform)):

```yaml
train:
  changes:
    url: "https://github.com/acme/payments-api/compare/{previous_version}...{version}"
    command: "git -C /repos/payments-api log --oneline {previous_version}..{version}"
```

- **`url`:** a compare link, shown as **"What's in this release"** on the departure page,
  in the New departure dialog as you type the version, and in the Slack departure
  message. Any `http(s)` URL works: GitHub, GitLab, Bitbucket, your own changelog.
- **`command`:** runs once when a train departs. Its output is saved with the departure
  and listed in the UI's **Changes** tab; `git log --oneline` lines show the short hash
  and message.
- **CLI:** `traintraintrain board --version V --changes` prints the same list.

Both use `{version}`, `{previous_version}` and `{train}`. They run once per distinct live
version: if the canary is already on `v1.3.0` and the other regions are on `v1.2.0`, you
get two lists, each naming its platforms (`$DT_PLATFORMS` holds them for the command).
A platform with no known version has nothing to compare against. The command is
informational only: if it fails, the error is recorded and the deploy carries on. It
also runs for dry runs and previews, so keep it read-only.

**Not in production yet.** Name the branch with the newest code and traintraintrain shows,
at any time, what's on it that production doesn't run, per platform:

```yaml
train:
  changes:
    command: "git -C /repos/payments fetch -q && git -C /repos/payments log --oneline {previous_version}..{version}"
    head: origin/main
```

It's the first section of the UI's Timetable page, with changes that only reach some
platforms marked, and `traintraintrain pending --changes` prints it. It's the same
comparison as a departure preview, with the branch in place of the version, so it works
with `track_versions_by: sha` too (the branch is resolved to its current commit).

**Monorepos:** limit the command to the folders the service is built from, so commits to
other services are left out:

```yaml
    command: "git -C /repos/mono log --oneline {previous_version}..{version} -- services/payments libs/billing"
```

Versions may contain `/`, so per-service tags such as `payments/v1.3.0` work as they are.

## Live versions per platform

traintraintrain records which version each platform runs, in the same storage as
everything else (the `train_platform_versions` and `train_platform_history` tables in
Postgres). It's updated when a platform's deploy succeeds or a rollback completes; failed
and aborted deploys go into the history only.

When a train departs, each platform:

- **compares** the new version with what it runs, so a region that's behind gets the
  longer change list
- **rolls back** to what it ran, through `{previous_version}` in `rollback_command`

`--previous-version` (the dialog's optional field) is now only used for platforms with no
known version, such as the very first departure. If you leave it out, the departure's
rollback target shown in the UI is the version most platforms ran.

**Before departing**, the New departure dialog shows each platform's live version and what
the typed version would change there. The CLI does the same:

```console
$ traintraintrain preview --version v1.4.0 --changes
  STOP         PLATFORM           LIVE NOW         CHANGES
  canary       us-west-2-canary   v1.3.0           2 change(s)
  atlantic     us-east-1          v1.3.0           2 change(s)
  worldwide    ap-northeast-1     v1.2.0           6 change(s)
  ...
```

**Ask the platform itself.** Deploys made outside traintraintrain (a hotfix, a rollback in
Argo CD) aren't recorded. Set `live_version_command` and the platform is asked first; it
gets the platform's `vars` and prints the version as its last line:

```yaml
train:
  live_version_command: >-
    kubectl --context {cluster} -n payments get deploy api
    -o jsonpath='{{.spec.template.spec.containers[0].image}}' | sed 's/.*://'
  live_version_timeout: 30s
```

If the answer differs from the record, the departure logs it and records the observed
version. If the command fails, the recorded version is used.

### Track versions by tag or by commit SHA

Choose per train what identifies a version:

```yaml
train:
  track_versions_by: sha          # default: tag
  resolve_command: "git -C /repos/payments rev-parse {version}^{{commit}}"
```

| | `tag` (default) | `sha` |
|---|---|---|
| Stored per platform | the version you deploy, plus its commit if `resolve_command` is set | the version and its commit |
| Change lists compare | version names (`v1.2.0..v1.3.0`) | commits, so they stay right if a tag is moved or re-pointed |
| Same commit under a new tag | a new version to deploy and compare | "already running it" |

- **`resolve_command`** turns a version into a commit SHA (its last line of output). It
  gets `{version}` and `{train}`; write `{{ }}` for literal braces, as in `^{{commit}}`.
  Without git on the machine, ask your forge, e.g. the GitHub API's
  `/repos/OWNER/REPO/commits/{version}`.
- **Deploying commit SHAs already?** Set `track_versions_by: sha` and leave out
  `resolve_command`: each version is its own SHA.
- In `sha` mode, the changes `url` and `command` get the commits as `{previous_version}` and
  `{version}`; the names are in `$DT_VERSION_NAME` and `$DT_PREVIOUS_VERSION_NAME`.
- Deploy, inspection and rollback commands can use `{sha}` and `{previous_sha}` in either
  mode.
- If a version can't be resolved, the departure says so and carries on, comparing by name.

SHAs are stored in the `sha` and `previous_sha` columns of both live tables, and shown
next to versions in the UI and in `live`, `preview` and `board`.

**Other commands:**

```bash
traintraintrain live                     # what each platform runs (--probe to ask them)
traintraintrain live --history           # every deploy, rollback and failure, newest first
traintraintrain live-set --platform us-east-1 --version v1.3.1   # record a hand-made change (--sha to give its commit)
```

The UI's Timetable page shows a **Live now** column and the same history. Existing
installs need nothing: until a platform has a record, its version is worked out from past
departures. With Postgres, the new tables are created automatically, or by `db migrate`.
