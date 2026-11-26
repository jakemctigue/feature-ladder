// Fingerprint of the Python reference this binary was ported from. Shared by
// build.rs (via include!) and the binary, so keep it free of crate paths.
//
// The binary refuses to decide anything when the fingerprint of the files next
// to it differs from the one compiled in: the Python guard runs instead, so a
// classifier change in Python can never be silently ignored by a stale binary.

/// (path under the plugin root, only the `def now():` block counts)
pub const REFERENCE_FILES: [(&str, bool); 4] = [
    ("hooks/scripts/guard_irreversible.py", false),
    ("skills/orchestration-ladder/scripts/irreversible.py", false),
    ("skills/orchestration-ladder/scripts/ladder_state.py", false),
    ("skills/orchestration-ladder/scripts/pr_checkpoint.py", true),
];

fn fnv1a(data: &[u8], mut h: u64) -> u64 {
    for &b in data {
        h ^= b as u64;
        h = h.wrapping_mul(0x0100_0000_01b3);
    }
    h
}

/// CRLF -> LF, so a Windows checkout with autocrlf hashes like a Linux one.
fn lf(text: &[u8]) -> Vec<u8> {
    let mut out = Vec::with_capacity(text.len());
    let mut i = 0;
    while i < text.len() {
        if text[i] == b'\r' && text.get(i + 1) == Some(&b'\n') {
            i += 1;
            continue;
        }
        out.push(text[i]);
        i += 1;
    }
    out
}

/// The `def now():` function of pr_checkpoint.py (its header line up to the
/// next line that starts at column 0).
fn now_block(text: &[u8]) -> Vec<u8> {
    let mut out = Vec::new();
    let mut inside = false;
    for line in text.split(|b| *b == b'\n') {
        if inside {
            if !line.is_empty() && !line[0].is_ascii_whitespace() {
                break;
            }
            out.extend_from_slice(line);
            out.push(b'\n');
        } else if line.starts_with(b"def now(") {
            inside = true;
            out.extend_from_slice(line);
            out.push(b'\n');
        }
    }
    out
}

/// None when a reference file cannot be read.
pub fn reference_fingerprint(root: &std::path::Path) -> Option<u64> {
    let mut h: u64 = 0xcbf2_9ce4_8422_2325;
    for (rel, only_now) in REFERENCE_FILES {
        let text = lf(&std::fs::read(root.join(rel)).ok()?);
        let part = if only_now { now_block(&text) } else { text };
        h = fnv1a(rel.as_bytes(), h);
        h = fnv1a(&[0], h);
        h = fnv1a(&part, h);
        h = fnv1a(&[0], h);
    }
    Some(h)
}
