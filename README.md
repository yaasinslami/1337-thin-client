# 1337 Thin Client

Native Brave and VS Code for a Linux 42/1337 workstation with a small HOME
filesystem. Keep reproducible applications and caches in **goinfre** while
preserving personal browser data and editor preferences in **HOME**.

Inspired by [shank187/session-setup](https://github.com/shank187/session-setup).
See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for attribution.

## Why

A browser profile, Flatpak caches, extensions and development dependencies can
consume a tiny HOME quota quickly. On some school workstations, the Flatpak
graphics runtime is incompatible with the host kernel/GPU combination. A native
browser can use the host graphics stack instead. This is a compatibility approach,
not a guarantee of hardware acceleration: verify your actual renderer.

```text
HOME                              GOINFRE (disposable)
  SSH keys/config (untouched)        thin-client/apps/{brave,vscode}/releases/
  shell/Git config (untouched)       thin-client/apps/*/current
  .config/BraveSoftware/             thin-client/cache/
    Brave-Browser/                  thin-client/vscode-extensions/
  .config/Code-Thin/                 thin-client/runtime/brave/{config,cache}/
                                    thin-client/tmp/
  .local/bin/ launch/setup scripts   thin-client/legacy-backups/
          |
          +-- VS Code Remote SSH --> optional remote server
                                      projects, Node, containers, databases,
                                      compilers, language servers, Codex
```

A remote server is **optional**. Native VS Code can edit local files too. This
tool does not install development toolchains, configure SSH, or contact a server.

Goinfre can disappear at any time. Keep your repository on a persistent Git
remote and important personal data in HOME or another durable backup. Goinfre
legacy backups are temporary fallbacks, not durable backups.

## Moving to another 1337 post

**HOME persists; goinfre is local to each workstation.** Your Brave profile,
Code-Thin state, SSH files and installed wrappers can survive while app binaries,
extensions and cache targets disappear. After quitting applications, restore them
explicitly from a normal host terminal:

```sh
~/.local/bin/thin-client-setup status
thin-client-setup prepare
brave-native
code-native
```

Status reports **New/unprepared workstation detected** when app executables are
missing. Clicking a launcher never downloads anything. Preparation recreates
missing cache targets behind existing correct links and restores applications
and extensions, including Brave runtime config/cache directories; it preserves
prepared profiles. No repository clone is needed on each new post: the installed setup
script is sufficient. If goinfre itself is unavailable,
locate the school's account-owned storage first. Detection prefers `~/goinfre`
when no explicit `GOINFRE` override is set; no username is hardcoded.

## Requirements

- Linux x86_64; Python 3.10+ from the system (stdlib only; no pip installation).
- `curl`, `rsync`, `ps`, `du`, `df`, `xdg-mime`, `xdg-settings`.
- Optional: Flatpak for old-app detection; `update-desktop-database` for menu cache
  refresh. Existing Flatpak support is used when present, never installed.
- A writable, account-owned goinfre directory outside HOME on another filesystem.
- At least 3 GiB free in goinfre for application staging, plus enough HOME space
  for a profile copy and a 200 MiB reserve. The old profile remains during testing.
- Host libraries compatible with the current official Brave/VS Code builds,
  a graphical desktop session, and working system HTTPS certificates.

Run from a normal host terminal. Container/Flatpak/Codex sandboxes may hide
processes, GPU nodes, the desktop bus or make goinfre read-only. The tool does not
escape these boundaries, change permissions on shared storage, or use sudo.

## Get and inspect the project

Clone a reviewed copy into your existing goinfre, not your constrained HOME.
For an account with the normal `~/goinfre` link:

```sh
test -d "$HOME/goinfre" || { echo 'Locate your goinfre first'; exit 1; }
mkdir -p "$HOME/goinfre/projects"
cd "$HOME/goinfre/projects"
git clone <reviewed-persistent-git-remote> 1337-thin-client
cd 1337-thin-client
less thin-client-setup
python3 -B -m unittest discover -s tests -v
./thin-client-setup audit
./thin-client-setup doctor
```

The remote placeholder must be replaced with your approved repository URL.
There is no remote shell installer and no `curl | bash` workflow.

Storage detection checks `$GOINFRE` first, then `~/goinfre`, then
`/goinfre/<HOME-directory-basename>`. Explicit invalid overrides fail clearly.
Detection uses `Path.home()` and the numeric UID, not a passwd username lookup.
There is **no large-binary fallback in HOME**.

## Prepare the applications

Save your work and fully quit old/native Brave and VS Code before migration or
updates. Brave can remain active after closing its last window; use its Quit action.
The tool reports running apps and refuses risky steps; it never force-kills them.

```sh
./thin-client-setup prepare --dry-run
./thin-client-setup prepare
```

Preparation installs the small setup script under `~/.local/bin`, downloads
official releases, verifies SHA-256, stages/version-controls app releases, copies
selected persistent data, installs Remote SSH and its small dependencies, then
writes managed launchers and the VS Code URI handler. It does **not launch apps**
or retire old profiles. Existing unmanaged destinations are refused.

Separate commands are available for troubleshooting:

```sh
./thin-client-setup install
./thin-client-setup migrate
./thin-client-setup integrate
./thin-client-setup status
```

Fresh accounts without Flatpak profiles get new empty native profiles. Accounts
with native destination data that this tool did not create require manual review;
it will not overwrite them. Existing source data is never moved or deleted by
migration. Partial failures are retained for inspection and block blind retries.

### Brave migration

Source: `~/.var/app/com.brave.Browser/config/BraveSoftware/Brave-Browser`.
Destination: `~/.config/BraveSoftware/Brave-Browser`.

Preserves bookmarks, history, login/cookie databases, Local State, preferences,
extensions and their state, IndexedDB/local storage and sessions. Omits known
caches only at explicitly approved relative paths, plus runtime locks and sockets.
Initial migration skips each supported profile’s entire `Service Worker` subtree,
including old symlinks; the source stays untouched. Existing native Service Worker
state is never managed or imported from goinfre. Anchored, directory-only cache
exclusions also preserve unknown
nested directories with cache-like names and unexpected files at cache locations.
A checksum-based rsync dry run verifies the persistent
copy before its staged directory is renamed into place.

Passwords/cookies may depend on the desktop keyring. Copying their files does not
prove that decryption works: manually check saved logins. No keyring or SSH key is
copied into goinfre. Browser contents and secret values are never printed.

### VS Code migration

Source: the Flatpak `config/Code/User` directory.
Destination: `~/.config/Code-Thin/User`.

Copies only settings, keybindings, user tasks and snippets when present. Supports
JSON settings with comments/trailing commas. A copied Flatpak-only terminal
profile using `flatpak-spawn`/`host-spawn` is removed; the original settings are
retained separately in HOME. Existing `~/.config/Code` is untouched.

Does not copy old extension binaries, workspace/history databases, caches or
logs. If you use custom VS Code profiles or extension-specific global state,
review/export them separately before retiring the old data. Settings Sync is a
convenient way to retain preferences, but can reinstall unwanted extensions;
review extension sync choices for this thin-client architecture.

The native client uses supported `--user-data-dir` and `--extensions-dir` options.
Only Remote SSH and its small extension pack are installed by default. Install
language servers/development extensions on the SSH remote when appropriate;
large C/C++ tooling is not migrated locally.

## Launch and desktop integration

```sh
~/.local/bin/brave-native
~/.local/bin/code-native
```

Or use **Brave (native thin client)** and **VS Code (native thin client)** in the
app menu. Launchers never download code. If goinfre was cleared, explicitly rerun
`~/.local/bin/thin-client-setup prepare` to restore reproducible app files.

No shell startup file is changed. Use full launcher paths, or add the personal
bin directory to PATH yourself after review.

The normal Code desktop entry opens files with `%F`. A separate hidden
`code-native-url-handler.desktop` calls the native wrapper with `--open-url %U`
and registers **only `x-scheme-handler/vscode`**. This routes browser callbacks
for GitHub authentication/Settings Sync to Code-Thin instead of Flatpak Code.

Browser defaults are changed **only on explicit request**. For this project's
native deployment, select Brave explicitly:

```sh
~/.local/bin/thin-client-setup defaults brave
# Or explicit consent during preparation:
./thin-client-setup prepare --make-default-browser
```

`defaults brave` installs/refreshes the managed launchers and entries, sets and
queries the browser/URI defaults, and refuses to report success if they disagree.
It is idempotent. `browser-default` remains available for compatibility and
requires entries to have been installed already. Plain `prepare` and `desktop`
do not take over HTTP/HTTPS defaults. To refresh menu
entries and the Code callback handler without downloads or extension installs:

```sh
~/.local/bin/thin-client-setup desktop
```

The supported external authentication route is:

```text
native Code -> HTTPS authorization page -> native Brave
native Brave -> vscode:// callback -> code-native-url-handler.desktop
             -> code-native --open-url <callback> -> Code-Thin
```

Desktop setup copies the official PNG bundled in the installed native Code
release into `~/.local/share/icons/hicolor/<size>x<size>/apps/code-native.png`.
The size comes from the bundled image (currently 1024×1024); no icon is downloaded
or borrowed from Flatpak. Both Code entries use the absolute persistent PNG path
in `Icon=` to avoid dependency on GNOME icon-theme lookup. `prepare`,
`integrate`, and `desktop` restore a missing icon and refresh user icon/desktop
caches when the corresponding utilities are available. `desktop` requires the
native Code release to be present; use `prepare` first on a new workstation.
Cache refresh is best-effort; missing or failing optional utilities do not block
setup. If GNOME still shows stale results, logout/login refreshes its shell cache.

Desktop setup preserves or creates user-level `Hidden=true` overrides for
`com.brave.Browser.desktop`, `org.mozilla.firefox.desktop`, and
`com.visualstudio.code.desktop`. It also discovers Microsoft Visual Studio Code
entries in standard system/XDG and Flatpak application directories and masks their
actual desktop IDs (for example `code.desktop`). Detection requires exact Code
identity in the main desktop group: `Name=Visual Studio Code`,
`X-Flatpak=com.visualstudio.code`, or the known Flatpak desktop ID. It never uses
substring matching, masks native thin-client launchers, changes system files,
uninstalls software, or hides unrelated editors such as Code::Blocks. Existing
unmanaged user launchers with conflicting IDs are refused rather than overwritten.
`status` and `doctor` report the persistent icon target and detected Code masks.

The normal `code-native.desktop` entry remains a file/folder launcher using `%F`.
Only the dedicated callback entry registers `x-scheme-handler/vscode`, with
`--open-url %U`. External HTTP/HTTPS URLs use `brave-native.desktop`, and
`xdg-settings get default-web-browser` must return that same entry.

Electron opens external URLs through the desktop's default applications;
[VS Code's URI integration](https://code.visualstudio.com/api/advanced-topics/remote-extensions#callbacks-and-uri-handlers)
supports native OAuth callbacks. Native Code drops an inherited `BROWSER`
variable from its own environment because `xdg-open`'s generic fallback can use
it. No browser executable is forced, and the shell environment is unchanged.
Desktop-handler queries and Code launches use HOME's `.config` and `.local/share`
for XDG configuration and desktop entries.

Run `thin-client-setup status` or `doctor` to see the actual default browser,
HTTP, HTTPS and vscode handlers alongside expected entries and clear mismatches.
Diagnostics only query; they do not authenticate or change associations. Fully
quit native Code before retesting so an existing process cannot retain a stale
environment. Check GitHub sign-in, Settings Sync and an ordinary external link
manually in the graphical session. Tests intercept XDG operations and callbacks;
they do not perform live OAuth or prove keyring/network/provider behavior.

## Cache policy

Brave receives process-local runtime directories under validated goinfre:

- `XDG_CONFIG_HOME=<thin-client-root>/runtime/brave/config`
- `XDG_CACHE_HOME=<thin-client-root>/runtime/brave/cache`

Both are created safely before launch and reconstructed by `prepare`. The real
profile remains `~/.config/BraveSoftware/Brave-Browser`, selected explicitly with
`--user-data-dir`; runtime configuration does not select another profile. This
avoids inherited runtime configuration causing Crashpad startup failures. No
global environment or shell configuration changes. Code and desktop-handler
commands retain their own environment. Known profile-local disposable caches
also point into goinfre.
The HTTP disk-cache target is approximately **100 MiB**, not a hard limit on all
browser storage. Persistent site databases, history and extensions can still grow.
No GPU forcing or sandbox-disabling flags are added.

VS Code's persistent user-data directory, including unsaved-file backups, remains
in HOME. Only specifically named generated cache/log directories use links into
goinfre; extensions use the supported CLI directory option. Existing non-link
cache contents are never silently replaced. Each launcher prepares only its own
application's caches and state; Brave does not require Code's extension directory.
Missing cache destinations are recreated at launch; unexpected links/data cause
a clear failure.

Brave cache setup uses an explicit relative-path table, not cache-name searches.
For `Default` and `Profile <number>`, it manages only direct `Cache`, `Code Cache`,
`GPUCache`, `DawnGraphiteCache`, `DawnWebGPUCache`, `GrShaderCache`, and `ShaderCache`
paths. The Brave root has a separate explicit table for those paths plus the
root-level rendering caches `GraphiteDawnCache` and `GPUPersistentCache`.
Existing real directories at those exact paths can be redirected after Brave is
fully stopped. Symlink ancestors and mount points are
refused. The directory is relocated to a unique backup under the validated
`<goinfre>/thin-client/cache-backups/`, its location is printed, then the
cache link is created. Cross-filesystem relocation copies the contents before
removing the original cache directory; a copy failure preserves the source.
Backups remain in disposable goinfre for manual review and are never
automatically pruned. No backup is stored in HOME. Other
existing directories/files and unexpected links are preserved and refused.
Persistent site databases and nested directories with cache-like names do not
qualify. Cache setup does not inspect, redirect, relocate, or validate as caches:

- `Service Worker/Database`, `Service Worker/ScriptCache`, and `Service Worker/CacheStorage`
- `IndexedDB`, `Local Storage`, `Session Storage`, and `WebStorage`
- Arbitrary nested paths such as `Some Application Data/Cache`, `Foo/ScriptCache`,
  and `Whatever/GPUCache`

These remain persistent HOME data. Their presence never blocks cache setup.
Migration and retirement backups use the same exact-path model for disposable
cache exclusions. Initial migration additionally skips Service Worker state;
explicit retirement backups preserve the source data. Full `migrate`, `integrate`, and `prepare`
still prepare both apps.

Existing prepared users need no additional profile migration for this cache fix:
after updating the installed setup script, Code launches independently and the
next Brave launch redirects allowlisted existing caches if Brave is stopped.
Profile or nested-cache mount points are refused as well as symlink ancestors.

Chromium's current [GPU cache subdirectory definitions](https://github.com/chromium/chromium/blob/main/gpu/ipc/common/gpu_disk_cache_type.cc)
explicitly name `GPUCache`, `DawnWebGPUCache`, and `DawnGraphiteCache`; all three
are in both our redirection and existing-directory relocation lists.
Its [browser rendering-cache locations](https://github.com/chromium/chromium/blob/main/chrome/browser/chrome_content_browser_client.cc)
also name root-level `ShaderCache`, `GrShaderCache`, `GraphiteDawnCache`, and
`GPUPersistentCache`; those exact root paths are explicitly supported.
An unapproved path is never offered to cache-link setup. A direct attempt to
manage one is refused before cache validation. No `Dawn*` or `*Cache*` pattern
grants relocation permission.

Service Worker storage is outside normal thin-client management. Missing paths
remain absent; existing directories and symlinks remain untouched. Brave may
recreate local site state naturally. No command restores old goinfre Service
Worker data into HOME or recreates the old redirection design.

Prior application releases and downloads are retained for rollback. Audit their
space periodically; this tool does not silently prune them.

## Mandatory manual verification

**Stop here before any retirement.**

Brave:
1. `brave://version`: executable is in goinfre; profile is in HOME.
2. Bookmarks/history/extensions/settings and saved passwords/logins are correct.
3. `brave://gpu`: inspect the renderer and hardware-accelerated features. Do not
   assume success if the renderer is llvmpipe/software rendering.
4. Test WebGL, WebGL2 and real workloads such as Figma.

VS Code:
1. Native UI and expected settings work.
2. GitHub sign-in opens the browser and returns to native Code; Settings Sync works.
3. If using a remote server, connect with Remote SSH, open a remote folder, check
   the SSH status indicator and run `hostname` in the integrated terminal.

The tool cannot perform these human checks for you.

## Optional Flatpak user-data retirement

Retirement **never runs during prepare**. It supports only the two known core
applications, not Firefox/Discord/Chrome or every directory under `.var`.

```sh
~/.local/bin/thin-client-setup retire-flatpak brave --dry-run
~/.local/bin/thin-client-setup retire-flatpak vscode --dry-run
```

Review the exact paths and sizes. After manual replacement verification, quit
both versions of the relevant app and explicitly confirm:

```sh
~/.local/bin/thin-client-setup retire-flatpak brave --verified
~/.local/bin/thin-client-setup retire-flatpak vscode --verified
```

Interactive confirmation requires the exact app ID. `--yes` supplies explicit
noninteractive confirmation; it still requires `--verified`. By default a
cache-excluded persistent-data copy is checksum-verified in goinfre before
deletion. `--no-backup` explicitly opts out. Verification failure aborts deletion.

Only `~/.var/app/com.brave.Browser` or `~/.var/app/com.visualstudio.code` can be
removed. A symlink path/ancestor is refused. System Flatpak binaries/runtimes are
not uninstalled and do not count as HOME recovery. Goinfre backup manifests can
contain personal paths; never commit them to this repository.

## Updates, failures and rollback

**`git pull` updates the checkout, not `~/.local/bin/thin-client-setup`.** Both
native wrappers execute that installed script. Upgrade from a reviewed checkout:

**For the first upgrade from an older installation, the `./` is required:** run
`./thin-client-setup install-self` from the checkout. Running
`thin-client-setup install-self` without `./` may invoke the old installed copy,
which can reject `install-self` as an invalid command even after `git pull`.
Once the repository command has replaced the installed copy, subsequent
`thin-client-setup ...` commands use the updated implementation (provided
`~/.local/bin` is on PATH). The full installed path below avoids PATH ambiguity.

```sh
cd ~/goinfre/projects/1337-thin-client
git pull
./thin-client-setup status
./thin-client-setup install-self
sha256sum ./thin-client-setup ~/.local/bin/thin-client-setup
./thin-client-setup status
# Refresh managed desktop entries and the Code callback, without downloads:
~/.local/bin/thin-client-setup desktop
# Explicit native browser choice:
~/.local/bin/thin-client-setup defaults brave
```

`install-self` copies only the reviewed script, using the existing managed-file
marker, atomic replacement, executable permissions and a post-copy hash check.
It refuses unmanaged files and symlink destinations. It works before goinfre apps
are prepared, changes no desktop associations, and is idempotent. `desktop` still
updates the script when invoked from the checkout, but the explicit command makes
that requirement clear. An installed script cannot discover every checkout;
run **the checkout's** `./thin-client-setup status` after pulling. It reports the
executing release, checkout HEAD, both SHA-256 hashes and `UPDATE REQUIRED` on a
mismatch. SHA-256 includes uncommitted edits; checkout HEAD alone does not.

For a friend's remaining DawnGraphiteCache error, collect these read-only facts
before concluding that a current implementation failed:

```sh
git rev-parse HEAD
sha256sum ./thin-client-setup ~/.local/bin/thin-client-setup
rg -n 'def brave_caches|def vscode_caches|DawnGraphiteCache|cache-backups|Existing data blocks' \
  ./thin-client-setup ~/.local/bin/thin-client-setup
ls -ld ~/.config/BraveSoftware/Brave-Browser/Default/DawnGraphiteCache
```

Different hashes indicate different code. The exact error alone cannot prove
which code ran. Current code relocates a stopped browser's normal directory at
that exact location, before generic link setup. A running Brave process produces
a quit-app error; invalid goinfre, mounts, files, and unexpected links produce
safe refusals. It never backs up these caches into HOME. Do not delete the cache
blindly; update the installed script, fully quit Brave (including background
processes), then retry `~/.local/bin/brave-native`.

Run `prepare` again after quitting applications. Official version metadata is
retrieved over HTTPS; downloads must match vendor SHA-256. Checksums trust the
vendor HTTPS metadata, not an independent signed release provenance chain.
Archive paths, special files, link escapes and link ancestors are checked.

Fully prepared app releases live under versioned directories. Activation replaces
only the managed `current` link atomically, retaining earlier releases. A failed
download/checksum/extraction does not replace that app's current release. If one
app succeeds and the second fails, the output reports where the run stopped;
rerun after fixing the cause. Source Flatpak data remains intact throughout.

For app-binary rollback, quit the app and review the retained release/receipt
before changing its `current` link. **Do not run an older Brave on a profile
upgraded by a newer browser.** Restore a separate compatible profile copy instead.
Temporary legacy backups are reference/recovery copies, not database fragments
to merge into an active profile. Before retirement, old Flatpak launchers remain
an independent fallback; new native changes are not synced back to them.

## Optional Codex storage: investigation and safe alternatives

The requested automatic persistent-state/disposable-package split is **deferred**.
This release adds a read-only storage diagnostic, without executing Codex:

```sh
~/.local/bin/thin-client-setup codex status
```

`codex install` and `codex prepare` deliberately refuse without downloading,
moving data or creating wrappers. Normal Brave/Code preparation never installs
Codex. Existing Codex installations, auth, config and databases remain untouched.

Current [official environment-variable documentation](https://learn.chatgpt.com/docs/config-file/environment-variables)
defines `CODEX_HOME` as the entire Codex state root, including config, auth,
sessions, logs, skills and standalone package metadata. It must already exist.
`CODEX_INSTALL_DIR` changes the visible command location; standalone packages
still live under `CODEX_HOME/packages/standalone`. These are separate concerns.
The local 0.161.0 layout is not a portable specification of future releases.

| Category | Persistence decision |
| --- | --- |
| Standalone executables and app-server packages | Reproducible payloads; no independent root for all managed packages was established in current official configuration |
| Downloads, generated caches and plugin package resources | Often reproducible, but not a supported single disposable subtree |
| Config and authentication | Persistent/sensitive; credentials may use files or the OS keyring |
| Sessions, history and SQLite-backed runtime state | User state; never blindly moved or marked disposable |
| User skills and plugin configuration | May be user-authored/persistent; plugin resources can mix reproducible and user state |

The [official configuration reference](https://learn.chatgpt.com/docs/config-file/config-reference)
documents `sqlite_home` and `log_dir`, but they do not supply a root for all heavy
packages. No selective package/daemon/plugin symlink scheme is implemented:
its safety across installers, self-updates and new posts has not been established.
The diagnostic reports presence of known paths only; it never prints auth/config
contents or opens databases.

Supported alternatives have explicit tradeoffs:

- Keep the normal persistent `CODEX_HOME` in HOME, with an independently installed
  CLI executable/package in goinfre. This moves that executable only and does not
  guarantee that managed daemon/plugin packages or sessions stop growing in HOME.
- Put an independent **whole** `CODEX_HOME` in detected goinfre. This reduces HOME
  usage but makes auth, config, sessions, skills and packages disposable together.
  A new post needs a new install and login; it does not meet the requested
  persistent-state requirement. The repo does not silently select this mode.
- Run Codex on a persistent remote machine, using local native Code over SSH.

Until a supported full split is established, this repository does not promise
both a tiny local Codex HOME and workstation-independent persistent Codex state.

## Troubleshooting

- Missing goinfre: locate/create your school-provided account directory and set
  `GOINFRE` explicitly if needed. The tool will not consume HOME for binaries.
- Read-only/sandbox errors: use a normal terminal on the host, not a Flatpak or
  restricted agent terminal. Do not bypass permissions with sudo.
- Existing data/cache conflict: inspect the named path. Do not delete it blindly.
- GitHub sign-in stuck: run `desktop`, check `xdg-mime query default
  x-scheme-handler/vscode`; optionally select `browser-default`. Then fully restart
  native Code and retry. `doctor` only reports mappings; it never authenticates.
- Browser profile lock: quit other instances and investigate ownership/stale
  workstation state. The tool intentionally does not delete locks automatically.
- Graphics still software-only: inspect `brave://gpu` and host drivers. Native
  packaging may solve a runtime mismatch but cannot fix every graphics problem.

## Development and privacy

```sh
python3 -B -m unittest discover -s tests -v
./thin-client-setup prepare --dry-run
```

Tests use temporary fixtures/mocks, never real profiles. They cover traversal and
link rejection, checksums, goinfre validation, cache/launcher safety, exact
retirement paths, JSONC, fresh accounts and read-only dry runs. Release tests run
the real self-install command in a temporary HOME, load that installed copy for
the friend's Brave-cache scenario, exercise the generated callback wrapper, and
simulate XDG defaults, cross-filesystem backups and empty-post recovery. External
auth and Codex installers are not executed. Development never applies setup to
the developer's live HOME.

Do not add real profiles, audit dumps, auth files, SSH material or legacy backups.
The repository ignores common generated/personal artifacts. Review every staged
file and commit before publishing; an ignore file is not a secret scanner.
