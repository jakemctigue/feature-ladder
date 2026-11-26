//! Port of skills/orchestration-ladder/scripts/irreversible.py: classify(),
//! gate() and record(), with the same patterns in the same order. The Python
//! file stays the reference; build.rs fingerprints it and the binary hands any
//! call to the Python guard when the file has changed since the build.
//!
//! Where Python would raise (an unexpected type in run.json), these functions
//! return Err(Bail): the Python guard then handles the call itself.

use crate::py::*;
use crate::pyjson::{s, Json};
use crate::shlex;

macro_rules! br {
    () => {
        r"(?:\s|:|\+|[\s:+]refs/heads/)"
    };
}
macro_rules! not_test {
    () => {
        r"(?!\S*(?:^|/|\\)(?:tests?|spec|__tests__)(?:/|\\))(?!\S*(?:^|/|\\)test_)(?!\S*[._-]test\.)(?!\S*\.spec\.)"
    };
}
macro_rules! value {
    () => {
        r#"("(?:\\.|[^"\\])*"|'[^']*'|[^\s;&|]+)"#
    };
}

static NEVER: [(L, &str); 14] = [
    (L::ci_need(r"\bgcloud\s+projects\s+delete\b", &[&["gcloud"], &["projects"], &["delete"]]), "delete a cloud project"),
    (L::ci_need(r"\bgcloud\s+firestore\s+databases\s+delete\b", &[&["gcloud"], &["firestore"], &["databases"], &["delete"]]), "delete a Firestore database"),
    (L::ci_need(r"\bgcloud\s+firestore\s+databases\s+update\b.*(no-enable-pitr|DELETE_PROTECTION_DISABLED)", &[&["gcloud"], &["firestore"], &["databases"], &["update"], &["no-enable-pitr", "delete_protection_disabled"]]), "weaken database protection"),
    (L::ci_need(r"\bgh\s+repo\s+delete\b", &[&["gh"], &["repo"], &["delete"]]), "delete a repository"),
    (L::ci_need(r"\bgh\s+pr\s+merge\b", &[&["gh"], &["pr"], &["merge"]]), "merge a PR"),
    (L::ci_need(concat!(r"\bgit\s+push\b[^;&|]*(--force\b|\s-f\b|--force-with-lease|\s\+)[^;&|]*", br!(), r"(main|master|production|release)(\s|$)"), &[&["git"], &["push"], &["-f", "+"], &["main", "master", "production", "release"]]), "force-push a shared branch"),
    (L::ci_need(r"\bgit\s+push\b[^;&|]*(\s:(main|master)(\s|$)|--delete\s+(main|master)(\s|$))", &[&["git"], &["push"], &["main", "master"], &[":", "--delete"]]), "delete main/master"),
    (L::ci_need(r"\b(gsutil\s+(-m\s+)?rm|gcloud\s+storage\s+rm)\b[^;&|]*backup", &[&["gsutil", "gcloud"], &["rm"], &["backup"]]), "delete backups"),
    (L::ci_need(r"\bterraform\s+destroy\b", &[&["terraform"], &["destroy"]]), "terraform destroy"),
    (L::ci_need(r"\brm\b(?=[^;&|]*\s(-[a-zA-Z]*[rR][a-zA-Z]*|--recursive)(\s|$))[^;&|]*\s(/\*?|~/?\*?|\$\{?HOME\}?/?\*?)(\s|$)", &[&["rm"], &["-"], &["/", "~", "$home", "${home"]]), "recursively delete / or the home folder"),
    (L::ci_need(r"\bdrop\s+database\b", &[&["drop"], &["database"]]), "drop a database"),
    (L::ci_need(r"\bgcloud\s+secrets\s+(delete|versions\s+(destroy|disable))\b", &[&["gcloud"], &["secrets"], &["delete", "versions"]]), "delete or disable a secret"),
    (L::ci_need(r"\bgcloud\s+secrets\s+(add-iam-policy-binding|set-iam-policy)\b", &[&["gcloud"], &["secrets"], &["add-iam-policy-binding", "set-iam-policy"]]), "change who can read a secret"),
    (L::ci_need(r"\bfirebase\s+functions:secrets:(destroy|prune)\b", &[&["firebase"], &["functions:secrets:"], &["destroy", "prune"]]), "destroy a Firebase secret"),
];
static PUSH_MAIN: L = L::ci_need(concat!(r"\bgit\s+push\b[^;&|]*", br!(), r"(main|master)(\s|$)"), &[&["git"], &["push"], &["main", "master"]]);

static OUTSIDE: [(L, &str, bool); 25] = [
    (L::ci_need(r"\bfirebase\s+firestore:delete\b", &[&["firebase"], &["firestore:delete"]]), "delete Firestore documents", true),
    (L::ci_need(r"\bfirebase\s+database:(set|remove|update|push)\b", &[&["firebase"], &["database:"]]), "write Realtime Database", true),
    (L::ci_need(r"\bfirebase\s+auth:import\b", &[&["firebase"], &["auth:import"]]), "import auth users", true),
    (L::ci_need(r"\bfirebase\s+functions:delete\b", &[&["firebase"], &["functions:delete"]]), "delete Cloud Functions", true),
    (L::ci_need(r"\bfirebase\s+deploy\b", &[&["firebase"], &["deploy"]]), "firebase deploy", false),
    (L::ci_need(r"\bfirebase\s+hosting:", &[&["firebase"], &["hosting:"]]), "hosting change", false),
    (L::ci_need(r"\bgcloud\s+firestore\s+import\b", &[&["gcloud"], &["firestore"], &["import"]]), "import into Firestore (overwrites)", true),
    (L::ci_need(r"\bgcloud\s+firestore\s+export\b", &[&["gcloud"], &["firestore"], &["export"]]), "export Firestore", false),
    (L::ci_need(r"\bgcloud\s+secrets\s+(create|versions\s+add|update)\b", &[&["gcloud"], &["secrets"], &["create", "versions", "update"]]), "write a secret", true),
    (L::ci_need(r"\bfirebase\s+functions:secrets:set\b", &[&["firebase"], &["functions:secrets:set"]]), "write a Firebase secret", true),
    (L::ci_need(r"\bgcloud\b[^;&|]*\s(delete|remove)\b", &[&["gcloud"], &["delete", "remove"]]), "delete cloud resource", true),
    (L::ci_need(r"\b(gsutil\s+(-m\s+)?rm|gcloud\s+storage\s+rm)\b", &[&["gsutil", "gcloud"], &["rm"]]), "delete stored objects", true),
    (L::ci_need(concat!(r"\b(node|tsx|ts-node|deno|bun|python3?|py|npx\s+\S+)\s+", not_test!(), r"(\S+[/\\])*\S*(migrat|backfill|purge|cleanup|clean-up|wipe|reset|dedupe|delete|import|seed)\S*\.(m?[jt]s|cjs|py)\b"), &[&["node", "tsx", "deno", "bun", "py", "npx"], &["migrat", "backfill", "purge", "cleanup", "clean-up", "wipe", "reset", "dedupe", "delete", "import", "seed"], &[".js", ".ts", ".mjs", ".mts", ".cjs", ".py"]]), "data script", true),
    (L::ci_need(r"\b(npm|pnpm|yarn)\s+(run\s+)?(\S*:)?(migrat|backfill|purge|cleanup|wipe|seed|db:reset|reset-db|reset:db)\S*", &[&["npm", "yarn"], &["migrat", "backfill", "purge", "cleanup", "wipe", "seed", "db:reset", "reset-db", "reset:db"]]), "data script", true),
    (L::ci_need(r"\b(drop\s+table|truncate\s+table|delete\s+from|alter\s+table\s+\S+\s+drop)\b", &[&["drop", "truncate", "delete", "alter"], &["table", "from"]]), "destructive SQL", true),
    (L::ci_need(r"\bupdate\s+\w+\s+set\b", &[&["update"], &["set"]]), "SQL update", true),
    (L::ci_need(r"\b(prisma\s+migrate\s+deploy|alembic\s+upgrade|knex\s+migrate|rails\s+db:migrate|manage\.py\s+migrate|flyway\s+migrate)\b", &[&["prisma", "alembic", "knex", "rails", "manage.py", "flyway"], &["migrate", "upgrade"]]), "schema migration", true),
    (L::ci_need(r"\b(terraform\s+apply|pulumi\s+up)\b", &[&["terraform", "pulumi"], &["apply", "up"]]), "infrastructure change", false),
    (L::ci_need(r"\bkubectl\s+delete\b|\bhelm\s+(uninstall|delete)\b", &[&["kubectl", "helm"], &["delete", "uninstall"]]), "delete cluster resources", true),
    (L::ci_need(r"\bkubectl\s+(apply|replace|scale|rollout)\b|\bhelm\s+(install|upgrade)\b", &[&["kubectl", "helm"], &["apply", "replace", "scale", "rollout", "install", "upgrade"]]), "cluster change", false),
    (L::ci_need(r"\b(npm|pnpm|yarn)\s+publish\b|\bcargo\s+publish\b|\btwine\s+upload\b|\bgem\s+push\b|\bdocker\s+push\b", &[&["publish", "upload", "push"]]), "publish", false),
    (L::ci_need(r"\b(vercel|netlify|fly|railway|wrangler)\b[^;&|]*(\bdeploy\b|(?<!\S)--prod(?:uction)?\b|\bup\b)", &[&["vercel", "netlify", "fly", "railway", "wrangler"], &["deploy", "--prod", "up"]]), "deploy", false),
    (L::ci_need(r"\bgh\s+release\s+create\b", &[&["gh"], &["release"], &["create"]]), "release", false),
    (L::ci_need(r"\bgit\s+push\b[^;&|]*(--force\b|\s-f\b|--force-with-lease)", &[&["git"], &["push"], &["-f"]]), "force push", false),
    (L::ci_need(r"\baws\s+\S+\s+(delete|terminate|remove|rm|rb)\S*|\baz\b[^;&|]*\sdelete\b", &[&["aws", "az"], &["delete", "terminate", "remove", "rm", "rb"]]), "delete cloud resource", true),
];

static CLOUD_TOOLS: L = L::ci_need(r"\b(firebase|gcloud|gsutil|aws|az|terraform|pulumi|kubectl|helm|vercel|netlify|fly|railway|wrangler)\b", &[&["firebase", "gcloud", "gsutil", "aws", "az", "terraform", "pulumi", "kubectl", "helm", "vercel", "netlify", "fly", "railway", "wrangler"]]);
static FIREBASE_TOOLS: L = L::ci_need(r"\b(firebase|gcloud|gsutil)\b", &[&["firebase", "gcloud", "gsutil"]]);
static SAFE_HINTS: [L; 6] = [
    L::ci_need(r"--dry-run\b", &[&["--dry-run"]]),
    L::ci_need(r"--dryrun\b", &[&["--dryrun"]]),
    L::ci_need(r"\bdry_run\b", &[&["dry_run"]]),
    L::ci_need(r"--preview\b", &[&["--preview"]]),
    L::ci_need(r"hosting:channel:deploy", &[&["hosting:channel:deploy"]]),
    L::ci_need(r"--only\s+firestore:indexes(?![^;&|]*--force)", &[&["--only"], &["firestore:indexes"]]),
];
static EMULATOR_HINTS: [L; 3] = [
    L::ci_need(r"emulators:(exec|start)", &[&["emulators:"], &["exec", "start"]]),
    L::ci_need(r"FIRESTORE_EMULATOR_HOST=", &[&["firestore_emulator_host="]]),
    L::ci_need(r"\blocalhost\b|127\.0\.0\.1", &[&["localhost", "127.0.0.1"]]),
];
static NONPROD_WORDS: L = L::ci_need(r"\b(dev|devel|development|test|testing|staging|stage|qa|sandbox|local|demo)\b", &[&["dev", "test", "stag", "qa", "sandbox", "local", "demo"]]);
static PROD_WORDS: L = L::ci_need(r"\b(prod|production|live)\b", &[&["prod", "live"]]);
static PROJECT_FLAG: L = L::new_need(r"(?:--project|-P)[=\s]+(\S+)", &[&["--project", "-p"]]);
static JS_TOOLS: L = L::ci_need(r"\b(node|tsx|ts-node|deno|bun|npx)\b", &[&["node", "tsx", "deno", "bun", "npx"]]);
static NPM_RUN: L = L::ci_need(r"\b(npm|pnpm|yarn)\s+run\b", &[&["npm", "yarn"], &["run"]]);

/// Our own scripts take commands and SQL as quoted arguments; a segment that RUNS
/// one is never classified. A segment that only names one (cat .env ledger.py) is.
const OWN_SCRIPTS: &[&str] = &[
    "irreversible", "pr_checkpoint", "intake", "route", "select_skills", "resolve_bank", "codex_review", "verify",
    "ledger", "confidence", "session_budget", "memory_gate", "brief", "with_secrets",
];
static INTERPRETER: L = L::new_need(r"^(python[\d.]*|py|pypy[\d.]*)$", &[&["py"]]);
static VAR_REF: L = L::new_need(r"(?<!\\)\$\{?([A-Za-z_][A-Za-z0-9_]*)", &[&["$"]]);

static HEREDOC: L = L::dotall_need(r#"(<<-?\s*['"]?(\w+)['"]?[^\n]*)\n.*?\n\s*\2\b"#, &[&["<<"], &["\n"]]);
static SHELL_HEREDOC: L = L::dotall_need(r#"\b(?:ba|z|da|k)?sh\b[^;&|\n]*<<-?\s*['"]?(\w+)['"]?[^\n]*\n(.*?)\n\s*\1\b"#, &[&["sh"], &["<<"], &["\n"]]);
static SINGLE_Q: L = L::new_need(r"'[^']*'", &[&["'"]]);
static DOUBLE_Q: L = L::new_need(r#""(?:\\.|[^"\\])*""#, &[&["\""]]);
static COMMENT: L = L::new_need(r"(^|\s)#[^\n]*", &[&["#"]]);
static SHELL_C: L = L::new_need(r#"\b(?:ba|z|da|k)?sh\s+(?:-[a-zA-Z]+\s+)*-[a-zA-Z]*c[a-zA-Z]*\s+(?:'([^']*)'|"((?:\\.|[^"\\])*)")"#, &[&["sh"], &["c"], &["'", "\""]]);
static EVAL: L = L::new_need(r#"\beval\s+(?:'([^']*)'|"((?:\\.|[^"\\])*)")"#, &[&["eval"]]);
static SUBST: L = L::new_need(r"\$\(([^()]*)\)|`([^`]*)`", &[&["$(", "`"]]);
static UNESCAPE: L = L::new_need(r#"\\(["\\$`])"#, &[&["\\"]]);
static AFTER_DASHDASH: L = L::dotall_need(r"\s--\s(.*)$", &[&["--"]]);
static GCLOUD_ACCESS: L = L::new_need(r"\bsecrets\s+versions\s+access\b", &[&["secrets"], &["versions"], &["access"]]);
static FIREBASE_ACCESS: L = L::new_need(r"\bfunctions:(secrets:access|config:get)\b", &[&["functions:"]]);
static DRIVE: L = L::new_need(r"[A-Za-z]:[\\/]", &[&[":"]]);
static SCHEME: L = L::new_need(r"\w+://", &[&["://"]]);
static EXT_END: L = L::new_need(r"\.\w{1,5}$", &[&["."]]);

static TOKEN_SHAPES: L = L::new_need(
    concat!(
        r"\b(sk-[A-Za-z0-9_-]{16,}|sk_(?:live|test)_[A-Za-z0-9]{16,}|rk_(?:live|test)_[A-Za-z0-9]{16,}|",
        r"gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|glpat-[A-Za-z0-9_-]{16,}|xox[abprs]-[A-Za-z0-9-]{10,}|",
        r"AKIA[0-9A-Z]{16}|AIza[0-9A-Za-z_-]{30,}|hsk_[A-Za-z0-9_-]{16,}|",
        r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{5,})"
    ),
    &[&["sk-", "sk_", "rk_", "gh", "github_pat_", "glpat-", "xox", "akia", "aiza", "hsk_", "eyj"]],
);
static URL_PASSWORD: L = L::new_need(r"(\w+://[^/\s:@]+):([^@\s/]+)@", &[&["://"], &["@"]]);
static BEARER: L = L::new_need(r"\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{8,}", &[&["bearer", "basic"]]);
static USER_PASS: L = L::new_need(r"(?<!\S)(-u|--user)(=|\s+)([^\s:]+):\S+", &[&["-u"], &[":"]]);
static GH_SECRET_BODY: L = L::new_need(concat!(r"(\bgh\s+secret\s+set\b.*?\s(?:--body|-b)(?:=|\s+))", value!()), &[&["gh"], &["secret"], &["set"], &["-b"]]);
static ASSIGNMENT: L = L::new_need(concat!(r#"(?:^|(?<=[\s;&|("']))([A-Za-z_][A-Za-z0-9_]*)="#, value!()), &[&["="]]);
static FLAG: L = L::new_need(concat!(r"(?<![\w-])(--[A-Za-z][\w-]*)(=|\s+)(?!-)", value!()), &[&["--"]]);
static OPAQUE: L = L::new(r"^(?=.*[a-z])(?=.*[A-Z])(?=.*\d)[A-Za-z0-9_+=/-]{24,}$|^[0-9a-fA-F]{32,}$");

const SECRET_WORDS: &[&str] = &["KEY", "APIKEY", "SECRET", "TOKEN", "PASSWORD", "PASSWD", "CREDENTIAL", "CREDENTIALS"];
const SECRET_ENDINGS: &[&str] = &["APIKEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD"];
const NOT_A_VALUE: &[&str] = &[
    "FILE", "FILES", "PATH", "DIR", "URL", "URI", "NAME", "NAMES", "ID", "IDS", "COUNT", "LEN", "LENGTH", "TTL",
    "TYPE", "HEADER", "PREFIX", "SUFFIX", "ENV", "VAR", "EXPIRY", "EXPIRES", "ENDPOINT", "HOST", "PORT", "VERSION",
    "ALG", "ALGORITHM", "SIZE", "FORMAT", "TIMEOUT", "LOCATION", "REGION", "ALIAS", "FIELD", "COLUMN", "USER",
    "USERNAME", "EMAIL", "SCOPE", "SCOPES", "ROTATION", "POLICY", "REF", "ARN",
];
const SECRET_FLAG_LAST: &[&str] = &["TOKEN", "PASSWORD", "PASSWD", "APIKEY"];
const SECRET_FLAG_PAIR: &[&str] = &["API", "SECRET", "ACCESS", "PRIVATE", "CLIENT", "AUTH", "SIGNING", "ENCRYPTION", "MASTER"];
const MIN_SECRET_CHARS: usize = 16;
const MIN_REDACT_CHARS: usize = 8;
const READERS: &[&str] = &[
    "cat", "type", "less", "more", "head", "tail", "bat", "nl", "tac", "grep", "egrep", "fgrep", "rg", "awk", "sed",
    "sort", "strings", "xxd", "od", "base64", "cut", "get-content", "gc",
];
const GREPS: &[&str] = &["grep", "egrep", "fgrep", "rg"];
const NOT_DOTENV: &[&str] = &["example", "sample", "template", "tmpl", "tpl", "dist", "defaults", "default", "schema", "md"];
const WRAPPERS: &[&str] = &[
    "sudo", "command", "exec", "time", "nohup", "builtin", "env", "do", "then", "else", "elif", "if", "while", "until",
];
const REDACTED: &str = "<redacted>";

fn is_in(set: &[&str], w: &str) -> bool {
    set.contains(&w)
}

/// ASSIGN.match(w) for `^([A-Za-z_][A-Za-z0-9_]*)=(.*)$` (re.S): (name, value).
/// The name cannot hold '=', so the first '=' splits; `(.*)$` takes the rest.
fn assign(w: &str) -> Option<(&str, &str)> {
    let (name, value) = w.split_once('=')?;
    let mut cs = name.chars();
    let first = cs.next()?;
    let ok = (first.is_ascii_alphabetic() || first == '_') && cs.all(|c| c.is_ascii_alphanumeric() || c == '_');
    ok.then_some((name, value))
}

/// OWN_SCRIPTS.match(s) for `^(names)\.py$` (Python's `$` allows one final newline).
fn own_name(s: &str) -> bool {
    let s = s.strip_suffix('\n').unwrap_or(s);
    s.strip_suffix(".py").is_some_and(|stem| OWN_SCRIPTS.contains(&stem))
}

// ------------------------------------------------------------ text shaping --

fn executable_text(cmd: &str) -> R<String> {
    let t = sub(&HEREDOC, cmd, |c| Ok(format!("{} ", group(c, 1))))?;
    let t = sub(&SINGLE_Q, &t, |_| Ok("''".into()))?;
    let t = sub(&DOUBLE_Q, &t, |_| Ok("\"\"".into()))?;
    sub(&COMMENT, &t, |_| Ok(" ".into()))
}

/// Split on unquoted ; & | and newlines; quoted text stays inside its segment.
fn segments(cmd: &str) -> Vec<String> {
    let chars: Vec<char> = cmd.chars().collect();
    let (mut segs, mut cur, mut q, mut i) = (Vec::new(), String::new(), None::<char>, 0usize);
    while i < chars.len() {
        let ch = chars[i];
        if ch == '\\' && q != Some('\'') && i + 1 < chars.len() {
            cur.push(ch);
            cur.push(chars[i + 1]);
            i += 2;
            continue;
        }
        if let Some(qc) = q {
            if ch == qc {
                q = None;
            }
            cur.push(ch);
        } else if ch == '\'' || ch == '"' {
            q = Some(ch);
            cur.push(ch);
        } else if matches!(ch, ';' | '&' | '|' | '\n') {
            segs.push(std::mem::take(&mut cur));
        } else {
            cur.push(ch);
        }
        i += 1;
    }
    segs.push(cur);
    segs.into_iter().filter(|g| has_text(g)).collect()
}

/// For each BYTE of cmd: the quote it is inside (' or "), or None. (Python
/// indexes code points; a byte mask lets regex byte offsets index it directly.)
fn quoted(cmd: &str) -> Vec<Option<char>> {
    let mut mask = vec![None; cmd.len()];
    let idx: Vec<(usize, char)> = cmd.char_indices().collect();
    let (mut q, mut i) = (None::<char>, 0usize);
    let set = |mask: &mut Vec<Option<char>>, k: usize, v: Option<char>| {
        let (b, c) = idx[k];
        for m in &mut mask[b..b + c.len_utf8()] {
            *m = v;
        }
    };
    while i < idx.len() {
        let ch = idx[i].1;
        if ch == '\\' && q != Some('\'') && i + 1 < idx.len() {
            set(&mut mask, i, q);
            set(&mut mask, i + 1, q);
            i += 2;
            continue;
        }
        if let Some(qc) = q {
            set(&mut mask, i, q);
            if ch == qc {
                q = None;
            }
        } else if ch == '\'' || ch == '"' {
            q = Some(ch);
            set(&mut mask, i, q);
        }
        i += 1;
    }
    mask
}

fn unescape(dq: &str) -> R<String> {
    sub(&UNESCAPE, dq, |c| Ok(group(c, 1).to_string()))
}

/// Code a shell runs that quote-stripping would hide.
fn inner_commands(raw: &str) -> R<Vec<String>> {
    let mask = quoted(raw);
    let mut inner = Vec::new();
    for rx in [&SHELL_C, &EVAL] {
        for m in find_all(rx, raw)? {
            let start = m.get(0).unwrap().start();
            if mask[start].is_none() {
                inner.push(match m.get(1) {
                    Some(g) => g.as_str().to_string(),
                    None => unescape(group(&m, 2))?,
                });
            }
        }
    }
    for m in find_all(&SUBST, raw)? {
        let start = m.get(0).unwrap().start();
        if mask[start] != Some('\'') && !raw[..start].ends_with('\\') {
            inner.push(match m.get(1) {
                Some(g) => g.as_str().to_string(),
                None => group(&m, 2).to_string(),
            });
        }
    }
    Ok(inner.into_iter().filter(|c| has_text(c)).collect())
}

fn rank(c: &Json) -> u8 {
    match c.get("decision").and_then(Json::as_str) {
        Some("deny") => 2,
        _ => 1,
    }
}

fn tier_rank(c: &Json) -> u8 {
    match c.get("tier").and_then(Json::as_str) {
        Some("secret") => 4,
        Some("never") => 3,
        Some("risky") => 2,
        Some("mundane") => 1,
        _ => 0,
    }
}

/// The more consequential of two classifications.
fn worse(a: Json, b: Json) -> Json {
    let (ra, rb) = (rank(&a), rank(&b));
    if ra != rb {
        return if ra > rb { a } else { b };
    }
    if tier_rank(&a) >= tier_rank(&b) {
        a
    } else {
        b
    }
}

fn strip_comment(seg: &str) -> &str {
    let mask = quoted(seg);
    let mut prev: Option<char> = None;
    for (i, ch) in seg.char_indices() {
        if ch == '#' && mask[i].is_none() && prev.is_none_or(isspace) {
            return &seg[..i];
        }
        prev = Some(ch);
    }
    seg
}

/// The words a shell would see: quotes removed, redirections as their own tokens.
fn tokens(seg: &str) -> Vec<String> {
    let seg = strip_comment(seg);
    match shlex::tokens(seg) {
        Ok(t) => t,
        Err(()) => split_ws(seg).into_iter().map(|t| strip_quotes(t).to_string()).collect(),
    }
}

fn base(word: &str) -> String {
    let last = word.rsplit(['\\', '/']).next().unwrap_or("");
    let b = last.to_lowercase();
    match b.strip_suffix(".exe") {
        Some(s) => s.to_string(),
        None => b,
    }
}

/// (command word, its arguments) after assignments, wrappers and redirections.
fn command(tokens: &[String]) -> R<(String, Vec<String>)> {
    let mut words: Vec<String> = Vec::new();
    let mut i = 0;
    while i < tokens.len() {
        let t = &tokens[i];
        if !t.is_empty() && t.chars().all(|c| c == '<' || c == '>') {
            if words.last().is_some_and(|w| isdigit(w)) {
                words.pop();
            }
            if t == "<" && i + 1 < tokens.len() {
                words.push(tokens[i + 1].clone());
            }
            i += 2;
            continue;
        }
        words.push(t.clone());
        i += 1;
    }
    let (mut i, mut after_wrapper) = (0usize, false);
    while i < words.len() {
        let w = &words[i];
        if assign(w).is_some() || matches!(w.as_str(), "(" | ")" | "{" | "!") {
            i += 1;
        } else if is_in(WRAPPERS, &base(w)) {
            i += 1;
            after_wrapper = true;
        } else if after_wrapper && w.starts_with('-') {
            i += 1;
        } else {
            break;
        }
    }
    if i >= words.len() {
        return Ok(if words.iter().any(|w| base(w) == "env") { ("env".into(), vec![]) } else { (String::new(), vec![]) });
    }
    let args = words[i + 1..].iter().filter(|w| *w != "(" && *w != ")").cloned().collect();
    Ok((base(&words[i]), args))
}

/// The feature-ladder script a segment RUNS (its file name), else None.
fn own_script(tokens: &[String]) -> R<Option<String>> {
    let (cmd, args) = command(tokens)?;
    if own_name(&cmd) {
        return Ok(Some(cmd));
    }
    let mut rest: Vec<&String> = args.iter().filter(|a| !a.starts_with('-')).collect();
    if (cmd == "bash" || cmd == "sh") && !rest.is_empty() && base(rest[0]) == "py.sh" {
        rest.remove(0);
    } else if !match_start(&INTERPRETER, &cmd)? {
        return Ok(None);
    }
    if let Some(first) = rest.first() {
        let b = base(first);
        if own_name(&b) {
            return Ok(Some(b));
        }
    }
    Ok(None)
}

// ------------------------------------------------------------ secrets ------

pub fn secret_name(name: &str) -> bool {
    if name != name.to_uppercase() {
        return false;
    }
    let parts: Vec<&str> = name.split('_').filter(|p| !p.is_empty()).collect();
    match parts.last() {
        None => return false,
        Some(last) if is_in(NOT_A_VALUE, last) => return false,
        _ => {}
    }
    if parts.contains(&"PUBLIC") {
        return false;
    }
    parts.iter().any(|p| is_in(SECRET_WORDS, p) || SECRET_ENDINGS.iter().any(|e| p.ends_with(e)))
}

pub fn secret_flag(flag: &str) -> bool {
    let up = flag.trim_matches('-').to_uppercase().replace('_', "-");
    let parts: Vec<&str> = up.split('-').filter(|p| !p.is_empty()).collect();
    let Some(last) = parts.last() else { return false };
    if is_in(SECRET_FLAG_LAST, last) || last.ends_with("APIKEY") || last.ends_with("PASSWORD") {
        return true;
    }
    (*last == "KEY" || *last == "SECRET") && parts.len() > 1 && is_in(SECRET_FLAG_PAIR, parts[parts.len() - 2])
}

fn literal(value: &str, min_chars: usize) -> R<bool> {
    let v = strip_quotes(value);
    if char_len(v) < min_chars
        || v.starts_with(['$', '/', '~', '<', '@', '.'])
        || match_start(&DRIVE, v)?
        || match_start(&SCHEME, v)?
    {
        return Ok(false);
    }
    Ok(!((v.contains('/') || v.contains('\\')) && search(&EXT_END, v)?))
}

fn dotenv(path: &str) -> bool {
    let b = path.rsplit(['\\', '/']).next().unwrap_or("").to_lowercase();
    if b == ".env" {
        return true;
    }
    if b.starts_with(".env.") || b.ends_with(".env") {
        return !b.split('.').any(|p| is_in(NOT_DOTENV, p));
    }
    false
}

/// The label of the secret exposure in one command segment, or None.
fn secret_exposure(seg: &str) -> R<Option<&'static str>> {
    let toks = tokens(seg);
    let (cmd, args) = command(&toks)?;
    let flags: Vec<&String> = args.iter().filter(|a| a.starts_with('-')).collect();
    let files: Vec<&String> = args.iter().filter(|a| !a.starts_with('-')).collect();
    let short: String = flags.iter().filter(|f| !f.starts_with("--")).flat_map(|f| f.chars().skip(1)).collect();
    if is_in(READERS, &cmd) && files.iter().any(|a| dotenv(a)) {
        let quiet = is_in(GREPS, &cmd)
            && (short.chars().any(|c| "qclL".contains(c))
                || flags.iter().any(|f| {
                    matches!(f.as_str(), "--quiet" | "--silent" | "--count" | "--files-with-matches" | "--files-without-match")
                }));
        let in_place = cmd == "sed" && flags.iter().any(|f| f.starts_with("-i") || f.starts_with("--in-place"));
        if !quiet && !in_place {
            return Ok(Some("prints a .env file"));
        }
    }
    if (cmd == "env" || cmd == "set") && args.is_empty() {
        return Ok(Some("dumps the environment"));
    }
    if cmd == "printenv" {
        if files.is_empty() {
            return Ok(Some("dumps the environment"));
        }
        if files.iter().any(|a| secret_name(a)) {
            return Ok(Some("prints a secret variable"));
        }
    }
    if matches!(cmd.as_str(), "export" | "declare" | "typeset") && files.is_empty() && flags.iter().all(|f| *f == "-p" || *f == "-x") {
        return Ok(Some("dumps the environment"));
    }
    if matches!(cmd.as_str(), "echo" | "printf" | "print") || (is_in(READERS, &cmd) && toks.iter().any(|t| t == "<<<")) {
        let text = strip_comment(seg);
        let mask = quoted(text);
        let live: String = text.char_indices().map(|(i, ch)| if mask[i] != Some('\'') { ch } else { ' ' }).collect();
        for m in find_all(&VAR_REF, &live)? {
            if secret_name(group(&m, 1)) {
                return Ok(Some("prints a secret variable"));
            }
        }
    }
    let joined = std::iter::once(cmd.as_str()).chain(args.iter().map(String::as_str)).collect::<Vec<_>>().join(" ");
    if cmd == "gcloud" && search(&GCLOUD_ACCESS, &joined)? {
        return Ok(Some("reads a secret value"));
    }
    if cmd == "firebase" && search(&FIREBASE_ACCESS, &joined)? {
        return Ok(Some("reads a secret value"));
    }
    for (i, t) in toks.iter().enumerate() {
        if let Some((name, value)) = assign(t) {
            if secret_name(name) && literal(value, MIN_SECRET_CHARS)? {
                return Ok(Some("puts a secret value on the command line"));
            }
        }
        if t.starts_with("--") {
            let (flag, mut value, eq) = match t.split_once('=') {
                Some((f, v)) => (f, v, true),
                None => (t.as_str(), "", false),
            };
            if !eq && i + 1 < toks.len() && !toks[i + 1].starts_with('-') {
                value = &toks[i + 1];
            }
            if !value.is_empty() && secret_flag(flag) && literal(value, MIN_SECRET_CHARS)? {
                return Ok(Some("puts a secret value on the command line"));
            }
        }
    }
    Ok(None)
}

fn deny(label: &str) -> Json {
    Json::Obj(vec![
        ("tier".into(), s("secret")),
        ("label".into(), s(label)),
        ("decision".into(), s("deny")),
        (
            "reason".into(),
            Json::Str(format!(
                "this {label}, which would put it in the transcript. Run the command that needs it \
                 through with_secrets.py --secret NAME -- <command> instead"
            )),
        ),
    ])
}

// ------------------------------------------------------------ classify -----

pub fn classify(cmd: &str, run: &Json, depth: u32) -> R<Json> {
    let inner: Vec<String> = find_all(&SHELL_HEREDOC, cmd)?.iter().map(|m| group(m, 2).to_string()).collect();
    let raw = sub(&HEREDOC, cmd, |c| Ok(format!("{} ", group(c, 1))))?;
    let mut kept: Vec<String> = Vec::new();
    for seg in segments(&raw) {
        match own_script(&tokens(&seg))?.as_deref() {
            Some("with_secrets.py") => {
                if let Some(m) = captures(&AFTER_DASHDASH, &seg)? {
                    kept.push(group(&m, 1).to_string());
                }
            }
            None => kept.push(seg),
            Some(_) => {}
        }
    }
    for seg in &kept {
        if let Some(label) = secret_exposure(seg)? {
            return Ok(deny(label));
        }
    }
    let raw = kept.join(" ; ");
    let mut out = classify_text(&executable_text(&raw)?, run)?;
    if depth < 4 {
        for c in inner.into_iter().chain(inner_commands(&raw)?) {
            out = worse(out, classify(&c, run, depth + 1)?);
        }
    }
    Ok(out)
}

/// env.get(key) on `run.get("environments") or {}`; Python raises when env is
/// not a dict, so that is a Bail.
fn env_get<'a>(env: &'a Json, key: &str) -> R<Option<&'a Json>> {
    match env {
        Json::Obj(_) => Ok(env.get(key)),
        _ => bail("run.json environments is not an object"),
    }
}

/// `needle in container` for a container from run.json (default []).
fn py_in(needle: &str, container: Option<&Json>) -> R<bool> {
    match container {
        None => Ok(false),
        Some(Json::Arr(a)) => Ok(a.iter().any(|e| e.as_str() == Some(needle))),
        Some(Json::Obj(o)) => Ok(o.iter().any(|(k, _)| k == needle)),
        Some(Json::Str(h)) => Ok(h.contains(needle)),
        Some(_) => bail("run.json environment list has an unexpected type"),
    }
}

fn project(cmd: &str, env: &Json) -> R<String> {
    let alias = if let Some(m) = captures(&PROJECT_FLAG, cmd)? {
        strip_quotes(group(&m, 1)).to_string()
    } else if search(&FIREBASE_TOOLS, cmd)? {
        match env_get(env, "default")? {
            Some(v) if v.truthy() => match v {
                Json::Str(d) => d.clone(),
                _ => return bail("environments.default is not a string"),
            },
            _ => String::new(),
        }
    } else {
        return Ok(String::new());
    };
    match env_get(env, "aliases")? {
        None => Ok(alias),
        Some(Json::Obj(o)) => match o.iter().find(|(k, _)| *k == alias) {
            None => Ok(alias),
            Some((_, Json::Str(p))) => Ok(p.clone()),
            Some(_) => bail("an environments.aliases value is not a string"),
        },
        Some(_) => bail("environments.aliases is not an object"),
    }
}

fn environment(cmd: &str, env: &Json) -> R<&'static str> {
    for p in &EMULATOR_HINTS {
        if search(p, cmd)? {
            return Ok("emulator");
        }
    }
    let proj = project(cmd, env)?;
    if proj.starts_with("demo-") {
        return Ok("emulator");
    }
    if !proj.is_empty() && py_in(&proj, env_get(env, "nonproduction")?)? {
        return Ok("nonprod");
    }
    if !proj.is_empty() && py_in(&proj, env_get(env, "production")?)? {
        return Ok("prod");
    }
    if search(&PROD_WORDS, cmd)? {
        return Ok("prod");
    }
    if search(&NONPROD_WORDS, cmd)? {
        return Ok("nonprod");
    }
    if !proj.is_empty() || search(&CLOUD_TOOLS, cmd)? {
        return Ok("prod");
    }
    if env_get(env, "production")?.is_some_and(Json::truthy)
        && search(&JS_TOOLS, cmd)?
        && !search(&NPM_RUN, cmd)?
    {
        return Ok("prod");
    }
    Ok("nonprod")
}

fn classify_text(text: &str, run: &Json) -> R<Json> {
    let empty = Json::Obj(vec![]);
    let env = match run.get("environments") {
        Some(v) if v.truthy() => v,
        _ => &empty,
    };
    let base_branch = match run.get("base") {
        Some(v) if v.truthy() => match v {
            Json::Str(b) => b.as_str(),
            _ => return bail("run.json base is not a string"),
        },
        _ => "",
    };
    for (p, label) in &NEVER {
        if search(p, text)? {
            return Ok(never(label));
        }
    }
    if !base_branch.is_empty()
        && !matches!(base_branch, "main" | "master" | "production" | "release")
        && may_contain(text, &[&["git"], &["push"]])
    {
        let pat = format!(r"\bgit\s+push\b[^;&|]*{}{}(\s|$)", br!(), fancy_regex::escape(base_branch));
        if search_re(&compile(&pat, true, false), text)? {
            return Ok(never(&format!("push to the base branch {base_branch}")));
        }
    }
    if search(&PUSH_MAIN, text)? {
        return Ok(never("push to main/master"));
    }
    let mut hit = None;
    for (p, label, destroys) in &OUTSIDE {
        if search(p, text)? {
            hit = Some((*label, *destroys));
            break;
        }
    }
    let Some((label, destroys)) = hit else {
        return Ok(Json::Obj(vec![("tier".into(), s("none")), ("decision".into(), s("allow"))]));
    };
    let place = environment(text, env)?;
    let mut safe = false;
    for p in &SAFE_HINTS {
        if search(p, text)? {
            safe = true;
            break;
        }
    }
    let risky = place == "prod" && destroys && !safe;
    Ok(Json::Obj(vec![
        ("label".into(), s(label)),
        ("environment".into(), s(place)),
        ("tier".into(), s(if risky { "risky" } else { "mundane" })),
        ("decision".into(), s("allow")),
        ("review".into(), s(if risky { "pending" } else { "n/a" })),
    ]))
}

fn never(label: &str) -> Json {
    Json::Obj(vec![
        ("tier".into(), s("never")),
        ("label".into(), s(label)),
        ("decision".into(), s("allow")),
        ("review".into(), s("pending")),
        ("reason".into(), Json::Str(format!("'{label}' runs and is logged for review"))),
    ])
}

// ------------------------------------------------------------ record -------

pub fn norm(cmd: &str) -> String {
    split_ws(cmd).join(" ")
}

fn review_status(c: &Json) -> &'static str {
    match c.get("review") {
        Some(Json::Str(r)) if r == "pending" => "pending",
        Some(Json::Str(r)) if r == "n/a" => "n/a",
        _ => match c.get("tier").and_then(Json::as_str) {
            Some("risky") | Some("never") => "pending",
            _ => "n/a",
        },
    }
}

/// The command with every secret-looking value replaced by <redacted>.
pub fn redact(cmd: &str) -> R<String> {
    let t = sub(&TOKEN_SHAPES, cmd, |_| Ok(REDACTED.into()))?;
    let t = sub(&URL_PASSWORD, &t, |m| Ok(format!("{}:{REDACTED}@", group(m, 1))))?;
    let t = sub(&BEARER, &t, |m| Ok(format!("{} {REDACTED}", group(m, 1))))?;
    let t = sub(&USER_PASS, &t, |m| Ok(format!("{}{}{}:{REDACTED}", group(m, 1), group(m, 2), group(m, 3))))?;
    let t = sub(&GH_SECRET_BODY, &t, |m| Ok(format!("{}{REDACTED}", group(m, 1))))?;
    let t = sub(&ASSIGNMENT, &t, |m| {
        let (name, value) = (group(m, 1), group(m, 2));
        let bare = strip_quotes(value);
        let secret = secret_name(&name.to_uppercase()) && literal(bare, MIN_REDACT_CHARS)?;
        Ok(if secret || match_start(&OPAQUE, bare)? { format!("{name}={REDACTED}") } else { group(m, 0).to_string() })
    })?;
    sub(&FLAG, &t, |m| {
        let (name, sep, value) = (group(m, 1), group(m, 2), group(m, 3));
        let bare = strip_quotes(value);
        let secret = secret_flag(name) && literal(bare, MIN_REDACT_CHARS)?;
        Ok(if secret || match_start(&OPAQUE, bare)? { format!("{name}{sep}{REDACTED}") } else { group(m, 0).to_string() })
    })
}

/// The text record() stores: redact(norm(cmd))[:300].
pub fn stored_cmd(cmd: &str) -> R<String> {
    Ok(prefix_chars(&redact(&norm(cmd))?, 300).to_string())
}

fn record(run: &mut Json, cmd: &str, c: &Json, status: &str, at: &str) -> R<()> {
    let entry = Json::Obj(vec![
        ("at".into(), s(at)),
        ("cmd".into(), Json::Str(stored_cmd(cmd)?)),
        ("label".into(), c.get("label").cloned().unwrap_or(Json::Null)),
        ("environment".into(), c.get("environment").cloned().unwrap_or(Json::Null)),
        ("tier".into(), c.get("tier").cloned().unwrap_or(Json::Null)),
        ("status".into(), s(status)),
        ("review".into(), s(review_status(c))),
    ]);
    let Json::Obj(fields) = run else { return bail("run is not an object") };
    if !fields.iter().any(|(k, _)| k == "outside_actions") {
        fields.push(("outside_actions".into(), Json::Arr(vec![])));
    }
    match fields.iter_mut().find(|(k, _)| k == "outside_actions") {
        Some((_, Json::Arr(list))) => {
            list.push(entry);
            Ok(())
        }
        _ => bail("run.json outside_actions is not a list"),
    }
}

/// (allowed, reason, run_changed), like irreversible.gate().
pub fn gate(cmd: &str, run: &mut Json, at: &str) -> R<(bool, String, bool)> {
    let c = classify(cmd, run, 0)?;
    if c.get("decision").and_then(Json::as_str) == Some("allow") {
        if c.get("tier").and_then(Json::as_str) == Some("none") {
            return Ok((true, String::new(), false));
        }
        let status = if review_status(&c) == "pending" { "ran (review: pending)" } else { "ran" };
        record(run, cmd, &c, status, at)?;
        return Ok((true, String::new(), true));
    }
    record(run, cmd, &c, "blocked: would expose a secret", at)?;
    let reason = c.get("reason").and_then(Json::as_str).unwrap_or("");
    Ok((false, format!("feature-ladder: {reason}."), true))
}
