//! feature-ladder PreToolUse(Bash) audit hook, in Rust.
//!
//! A port of hooks/scripts/guard_irreversible.py plus irreversible.gate(): same
//! input, same run.json bytes, same deny output, same lock as ladder_state.py,
//! so Python and Rust processes can log into one run at the same time. It
//! exists to skip the Python start-up on every Bash call.
//!
//! The Python files stay the reference. This binary hands the call to the
//! Python guard (same stdin) whenever it cannot promise the same result:
//!   - the Python reference changed since the build (fingerprint mismatch),
//!   - input Python would read differently (invalid UTF-8, a lone surrogate,
//!     a non-string command, unexpected types in run.json),
//!   - a regex hit the backtracking limit, or the port panicked before writing.
//!
//! Usage: guard_irreversible               the hook (payload on stdin)
//!        guard_irreversible --check       is the binary current? (exit 1 if stale)
//!        guard_irreversible --classify-batch   test aid: JSON lines {"cmd","run"} in,
//!                                         {"classify","cmd"} (or {"bail"}) out
//! FEATURE_LADDER_GUARD_TRACE=1 prints why a call was handed to Python (stderr).

mod classify;
mod fingerprint;
mod py;
mod pyjson;
mod shlex;

use py::{bail, isspace, Bail, R};
use pyjson::{s, Json, ParseError};
use std::ffi::OsStr;
use std::fs::{self, OpenOptions};
use std::io::{self, ErrorKind, Read, Write};
use std::panic::{self, AssertUnwindSafe};
use std::path::{Path, PathBuf};
use std::process::{self, Command, Stdio};
use std::sync::atomic::{AtomicBool, Ordering};
use std::thread::sleep;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const FINGERPRINT: &str = env!("GUARD_PY_FINGERPRINT");
const LOCK_STALE_SECONDS: f64 = 30.0;
const LOCK_TIMEOUT: Duration = Duration::from_secs(15);

/// Set just before run.json is written: after that, never hand the call to
/// Python (it would log the command a second time).
static WROTE: AtomicBool = AtomicBool::new(false);

fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        None => hook(),
        Some("--check") => process::exit(check()),
        Some("--classify-batch") => process::exit(classify_batch()),
        Some("--version") => {
            println!("guard_irreversible {} (python reference {FINGERPRINT})", env!("CARGO_PKG_VERSION"));
        }
        Some(_) => {
            eprintln!("usage: guard_irreversible [--check | --classify-batch | --version]  (the hook reads stdin)");
            process::exit(2);
        }
    }
}

fn trace(msg: &str) {
    if std::env::var_os("FEATURE_LADDER_GUARD_TRACE").is_some() {
        eprintln!("guard_irreversible: {msg}");
    }
}

// ------------------------------------------------------------ the hook -----

fn hook() -> ! {
    let mut payload = Vec::new();
    let _ = io::stdin().read_to_end(&mut payload);
    let Some(root) = plugin_root() else {
        eprintln!("feature-ladder: the outside-repo audit log skipped this command (plugin folder not found)");
        process::exit(0);
    };
    match fingerprint::reference_fingerprint(&root) {
        Some(fp) if format!("{fp:016x}") == FINGERPRINT => {}
        _ => delegate(&root, &payload, "the Python reference changed since this binary was built"),
    }
    panic::set_hook(Box::new(|info| trace(&format!("panic: {info}"))));
    match panic::catch_unwind(AssertUnwindSafe(|| run_hook(&payload))) {
        Ok(Ok(())) => process::exit(0),
        Ok(Err(Bail(why))) => delegate(&root, &payload, &why),
        Err(_) if WROTE.load(Ordering::SeqCst) => process::exit(0),
        Err(_) => delegate(&root, &payload, "the Rust guard panicked"),
    }
}

/// guard_irreversible.main(). Ok(()) = done (exit 0); Err = let Python decide.
fn run_hook(payload: &[u8]) -> R<()> {
    let Ok(text) = std::str::from_utf8(payload) else { return bail("stdin is not UTF-8") };
    let payload = match pyjson::loads(text.trim_start_matches('\u{feff}')) {
        Ok(v) => v,
        Err(ParseError::Invalid) => return Ok(()),
        Err(ParseError::Unsupported(why)) => return bail(why),
    };
    if !matches!(payload, Json::Obj(_)) || payload.get("tool_name") != Some(&s("Bash")) {
        return Ok(());
    }
    let cmd = match payload.get("tool_input") {
        Some(ti) if ti.truthy() => match ti {
            Json::Obj(_) => match ti.get("command") {
                None => String::new(),
                Some(Json::Str(c)) => c.clone(),
                Some(_) => return bail("tool_input.command is not a string"),
            },
            _ => return Ok(()), // Python: AttributeError, swallowed
        },
        _ => String::new(),
    };
    if cmd.is_empty() {
        return Ok(());
    }
    let cwd = match payload.get("cwd") {
        Some(v) if v.truthy() => match v {
            Json::Str(c) => PathBuf::from(c),
            _ => return Ok(()), // Python: TypeError from Path(), swallowed
        },
        _ => match std::env::current_dir() {
            Ok(d) => d,
            Err(_) => return Ok(()),
        },
    };
    let Some((root, f)) = find_run(&cwd) else { return Ok(()) };
    if !active(read_json(&f)?.as_ref()) {
        return Ok(()); // no run, a finished run, or an unreadable file: nothing to audit
    }
    // Classify and append under the lock, on a fresh read: two commands logged
    // at the same moment (parallel workers) must both end up in the file.
    let Some(lock) = Lock::acquire(&root) else { return Ok(()) };
    let Some(mut run) = read_json(&f)? else { return Ok(()) };
    if !active(Some(&run)) {
        return Ok(());
    }
    let (allowed, reason, changed) = classify::gate(&cmd, &mut run, &now_iso())?;
    if changed {
        WROTE.store(true, Ordering::SeqCst);
        let _ = write_json(&f, &run);
    }
    drop(lock);
    if !allowed {
        let out = Json::Obj(vec![(
            "hookSpecificOutput".into(),
            Json::Obj(vec![
                ("hookEventName".into(), s("PreToolUse")),
                ("permissionDecision".into(), s("deny")),
                ("permissionDecisionReason".into(), Json::Str(reason)),
            ]),
        )]);
        let newline = if cfg!(windows) { "\r\n" } else { "\n" }; // Python's text-mode stdout
        let mut stdout = io::stdout().lock();
        let _ = write!(stdout, "{}{newline}", pyjson::dumps_compact(&out));
        let _ = stdout.flush();
    }
    Ok(())
}

fn active(run: Option<&Json>) -> bool {
    matches!(run, Some(r @ Json::Obj(_)) if r.get("active").is_some_and(Json::truthy))
}

/// ladder_state.read_json(path): None when missing, unreadable or not JSON.
fn read_json(path: &Path) -> R<Option<Json>> {
    let Ok(bytes) = fs::read(path) else { return Ok(None) };
    let Ok(text) = std::str::from_utf8(&bytes) else { return Ok(None) }; // Python raises; the guard exits 0
    match pyjson::loads(text.strip_prefix('\u{feff}').unwrap_or(text)) {
        Ok(v) => Ok(Some(v)),
        Err(ParseError::Invalid) => Ok(None),
        Err(ParseError::Unsupported(why)) => bail(why),
    }
}

/// ladder_state.write_json: temp file in the same folder, then replace.
fn write_json(path: &Path, data: &Json) -> io::Result<()> {
    let text = pyjson::dumps_indent(data, 2) + "\n";
    let parent = path.parent().unwrap_or(Path::new("."));
    fs::create_dir_all(parent)?;
    let nanos = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let name = path.file_name().unwrap_or(OsStr::new("run.json")).to_string_lossy();
    let tmp = path.with_file_name(format!("{name}.{}.{nanos}.tmp", process::id()));
    let result = fs::write(&tmp, text.as_bytes()).and_then(|()| replace(&tmp, path));
    if tmp.exists() {
        let _ = fs::remove_file(&tmp);
    }
    result
}

fn replace(tmp: &Path, path: &Path) -> io::Result<()> {
    // Windows refuses the swap while another process holds the target open; retry briefly.
    let mut attempt = 0;
    loop {
        match fs::rename(tmp, path) {
            Err(e) if e.kind() == ErrorKind::PermissionDenied && attempt < 19 => {
                attempt += 1;
                sleep(Duration::from_millis(20));
            }
            other => return other,
        }
    }
}

/// ladder_state.locked(root): one writer at a time across processes. A lock
/// older than 30 s is taken over; after 15 s the caller proceeds unlocked.
struct Lock {
    path: PathBuf,
    held: bool,
}

impl Lock {
    fn acquire(root: &Path) -> Option<Lock> {
        let dir = root.join(".feature-ladder");
        fs::create_dir_all(&dir).ok()?; // Python raises here; the guard exits 0
        let path = dir.join("state.lock");
        let deadline = Instant::now() + LOCK_TIMEOUT;
        let mut held = false;
        loop {
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(mut fh) => {
                    if fh.write_all(process::id().to_string().as_bytes()).is_ok() {
                        held = true;
                        break;
                    }
                }
                Err(e) if e.kind() == ErrorKind::AlreadyExists => {
                    let Ok(mtime) = fs::metadata(&path).and_then(|m| m.modified()) else { continue };
                    let age = match SystemTime::now().duration_since(mtime) {
                        Ok(d) => d.as_secs_f64(),
                        Err(_) => 0.0, // modified in the future: not stale
                    };
                    if age > LOCK_STALE_SECONDS {
                        let _ = fs::remove_file(&path);
                        continue;
                    }
                }
                Err(_) => {} // e.g. access denied while another process deletes it
            }
            if Instant::now() >= deadline {
                break;
            }
            sleep(Duration::from_millis(10));
        }
        Some(Lock { path, held })
    }
}

impl Drop for Lock {
    fn drop(&mut self) {
        if self.held {
            let _ = fs::remove_file(&self.path);
        }
    }
}

/// datetime.now(timezone.utc).isoformat(timespec="seconds")
fn now_iso() -> String {
    let secs = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (days, rem) = (secs.div_euclid(86_400), secs.rem_euclid(86_400));
    // civil_from_days (Howard Hinnant)
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let m = if mp < 10 { mp + 3 } else { mp - 9 };
    let y = yoe + era * 400 + i64::from(m <= 2);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}+00:00", rem / 3600, rem % 3600 / 60, rem % 60)
}

// ------------------------------------------------------------ find_run -----

/// Path(p).resolve() (non-strict): canonicalise the longest existing prefix.
fn py_resolve(p: &Path) -> PathBuf {
    let abs = std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf());
    let mut tail = Vec::new();
    let mut cur = abs.clone();
    loop {
        if let Ok(c) = fs::canonicalize(&cur) {
            let mut out = strip_verbatim(c);
            for t in tail.iter().rev() {
                out.push(t);
            }
            return out;
        }
        match (cur.file_name().map(|n| n.to_os_string()), cur.parent().map(Path::to_path_buf)) {
            (Some(n), Some(parent)) => {
                tail.push(n);
                cur = parent;
            }
            _ => return abs,
        }
    }
}

#[cfg(windows)]
fn strip_verbatim(p: PathBuf) -> PathBuf {
    let Some(text) = p.to_str() else { return p };
    if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
        return PathBuf::from(format!(r"\\{rest}"));
    }
    if let Some(rest) = text.strip_prefix(r"\\?\") {
        let b = rest.as_bytes();
        if b.len() >= 2 && b[0].is_ascii_alphabetic() && b[1] == b':' {
            return PathBuf::from(rest);
        }
    }
    p
}

#[cfg(not(windows))]
fn strip_verbatim(p: PathBuf) -> PathBuf {
    p
}

/// (root, run.json) of the run this folder belongs to. Only a linked worktree
/// or a submodule (where .git is a file) asks git where the main checkout is.
fn find_run(cwd: &Path) -> Option<(PathBuf, PathBuf)> {
    let p = py_resolve(cwd);
    for d in p.ancestors() {
        let f = d.join(".feature-ladder").join("run.json");
        if f.is_file() {
            return Some((d.to_path_buf(), f));
        }
        let dot_git = d.join(".git");
        if dot_git.is_dir() {
            return None; // the main checkout itself, and it has no run
        }
        if dot_git.exists() {
            let common = git_common_dir(d)?;
            if common.file_name() == Some(OsStr::new(".git")) {
                let main = py_resolve(common.parent()?);
                let f = main.join(".feature-ladder").join("run.json");
                if f.is_file() {
                    return Some((main, f));
                }
            }
            return None;
        }
    }
    None
}

/// `git rev-parse --path-format=absolute --git-common-dir`, 10 s timeout.
fn git_common_dir(dir: &Path) -> Option<PathBuf> {
    let mut child = Command::new("git")
        .args(["rev-parse", "--path-format=absolute", "--git-common-dir"])
        .current_dir(dir)
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .spawn()
        .ok()?;
    let mut out = child.stdout.take()?;
    let reader = std::thread::spawn(move || {
        let mut v = Vec::new();
        let _ = out.read_to_end(&mut v);
        v
    });
    let deadline = Instant::now() + Duration::from_secs(10);
    let status = loop {
        match child.try_wait() {
            Ok(Some(st)) => break st,
            Ok(None) if Instant::now() < deadline => sleep(Duration::from_millis(2)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    };
    let bytes = reader.join().ok()?;
    if !status.success() {
        return None;
    }
    let text = String::from_utf8(bytes).ok()?;
    let t = text.trim_matches(isspace);
    (!t.is_empty()).then(|| PathBuf::from(t))
}

// ------------------------------------------------------------ plumbing -----

/// <root>/hooks/bin/guard_irreversible[.exe] -> <root>; else $CLAUDE_PLUGIN_ROOT.
fn plugin_root() -> Option<PathBuf> {
    let marker = |r: &Path| r.join(fingerprint::REFERENCE_FILES[0].0).is_file();
    if let Ok(exe) = std::env::current_exe() {
        if let Some(r) = exe.parent().and_then(Path::parent).and_then(Path::parent) {
            if marker(r) {
                return Some(r.to_path_buf());
            }
        }
    }
    let r = PathBuf::from(std::env::var_os("CLAUDE_PLUGIN_ROOT")?);
    marker(&r).then_some(r)
}

fn slashed(p: PathBuf) -> String {
    let t = p.to_string_lossy().into_owned();
    if cfg!(windows) {
        t.replace('\\', "/")
    } else {
        t
    }
}

#[cfg(windows)]
fn find_bash() -> Option<PathBuf> {
    if let Some(b) = std::env::var_os("FEATURE_LADDER_BASH").map(PathBuf::from) {
        if b.is_file() {
            return Some(b);
        }
    }
    // Never WSL's System32 bash or a Store alias: they run in another filesystem.
    if let Some(path) = std::env::var_os("PATH") {
        for dir in std::env::split_paths(&path) {
            let c = dir.join("bash.exe");
            let low = c.to_string_lossy().to_lowercase();
            if low.contains("\\system32\\") || low.contains("\\windowsapps\\") {
                continue;
            }
            if c.is_file() {
                return Some(c);
            }
        }
    }
    [r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files\Git\usr\bin\bash.exe"]
        .into_iter()
        .map(PathBuf::from)
        .find(|c| c.is_file())
}

#[cfg(not(windows))]
fn find_bash() -> Option<PathBuf> {
    Some(PathBuf::from("bash"))
}

/// Run the Python guard (through py.sh, like the hook did before) on the same
/// payload, and exit with nothing else said.
fn delegate(root: &Path, payload: &[u8], why: &str) -> ! {
    trace(&format!("handing the call to the Python guard: {why}"));
    if let Some(bash) = find_bash() {
        let spawned = Command::new(bash)
            .arg(slashed(root.join("hooks/scripts/py.sh")))
            .arg(slashed(root.join("hooks/scripts/guard_irreversible.py")))
            .stdin(Stdio::piped())
            .spawn();
        if let Ok(mut child) = spawned {
            if let Some(mut stdin) = child.stdin.take() {
                let _ = stdin.write_all(payload);
            }
            let _ = child.wait();
            process::exit(0);
        }
    }
    eprintln!("feature-ladder: the outside-repo audit log skipped this command (the Python guard could not be started)");
    process::exit(0);
}

fn check() -> i32 {
    let Some(root) = plugin_root() else {
        eprintln!("guard_irreversible: plugin folder not found (expected <plugin>/hooks/bin/)");
        return 1;
    };
    let now = fingerprint::reference_fingerprint(&root).map(|f| format!("{f:016x}"));
    if now.as_deref() == Some(FINGERPRINT) {
        println!("guard_irreversible: current (python reference {FINGERPRINT})");
        0
    } else {
        println!(
            "guard_irreversible: STALE - built against {FINGERPRINT}, the Python files are now {}; \
             the hook runs the Python guard until you rebuild (hooks/scripts/build_guard.sh)",
            now.as_deref().unwrap_or("unreadable")
        );
        1
    }
}

fn classify_batch() -> i32 {
    let mut input = String::new();
    if io::stdin().read_to_string(&mut input).is_err() {
        return 1;
    }
    let mut out = io::stdout().lock();
    for line in input.lines().filter(|l| !l.trim().is_empty()) {
        let result = (|| -> R<Json> {
            let item = pyjson::loads(line).map_err(|e| Bail(format!("bad input line: {e:?}")))?;
            let cmd = item.get("cmd").and_then(Json::as_str).unwrap_or("").to_string();
            let run = item.get("run").cloned().unwrap_or(Json::Obj(vec![]));
            let c = classify::classify(&cmd, &run, 0)?;
            Ok(Json::Obj(vec![("classify".into(), c), ("cmd".into(), Json::Str(classify::stored_cmd(&cmd)?))]))
        })();
        let v = result.unwrap_or_else(|Bail(why)| Json::Obj(vec![("bail".into(), Json::Str(why))]));
        let _ = writeln!(out, "{}", pyjson::dumps_compact(&v));
    }
    0
}
