//! Python str and re semantics the classifier relies on.

use fancy_regex::{Captures, Regex, RegexBuilder};
use std::sync::OnceLock;

/// Why the Rust guard hands a call to the Python guard instead of deciding it.
#[derive(Debug)]
pub struct Bail(pub String);

pub type R<T> = Result<T, Bail>;

pub fn bail<T>(why: impl Into<String>) -> R<T> {
    Err(Bail(why.into()))
}

// ------------------------------------------------------------------ str ------

/// str.isspace() for one character (Unicode White_Space plus U+001C..U+001F).
pub fn isspace(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// str.split() with no arguments.
pub fn split_ws(s: &str) -> Vec<&str> {
    s.split(isspace).filter(|p| !p.is_empty()).collect()
}

/// str.strip() with no arguments, as a test: is anything left?
pub fn has_text(s: &str) -> bool {
    s.chars().any(|c| !isspace(c))
}

/// s.strip("'\"")
pub fn strip_quotes(s: &str) -> &str {
    s.trim_matches(|c| c == '\'' || c == '"')
}

/// str.isdigit(): non-empty, every character a digit.
pub fn isdigit(s: &str) -> bool {
    !s.is_empty() && s.chars().all(|c| c.is_ascii_digit() || (!c.is_ascii() && c.is_numeric()))
}

/// s[:n] in code points.
pub fn prefix_chars(s: &str, n: usize) -> &str {
    match s.char_indices().nth(n) {
        Some((i, _)) => &s[..i],
        None => s,
    }
}

pub fn char_len(s: &str) -> usize {
    s.chars().count()
}

// ------------------------------------------------------------------ re -------

/// A lazily compiled pattern written in Python `re` syntax.
/// Literals every match must contain, as AND-of-ORs over the case-folded text
/// (see `folded`). A pattern whose literals are absent is not even compiled:
/// compiling ~40 Unicode-aware patterns would cost more than the Python start-up
/// this binary exists to save. Being too permissive is harmless; a literal that
/// a match could lack would be a bug (the parity tests compare with Python).
pub type Need = &'static [&'static [&'static str]];

pub struct L {
    pat: &'static str,
    ci: bool,
    dotall: bool,
    need: Need,
    cell: OnceLock<Regex>,
}

impl L {
    pub const fn new(pat: &'static str) -> Self {
        L { pat, ci: false, dotall: false, need: &[], cell: OnceLock::new() }
    }
    pub const fn new_need(pat: &'static str, need: Need) -> Self {
        L { pat, ci: false, dotall: false, need, cell: OnceLock::new() }
    }
    pub const fn ci_need(pat: &'static str, need: Need) -> Self {
        L { pat, ci: true, dotall: false, need, cell: OnceLock::new() }
    }
    pub const fn dotall_need(pat: &'static str, need: Need) -> Self {
        L { pat, ci: false, dotall: true, need, cell: OnceLock::new() }
    }
    pub fn get(&self) -> &Regex {
        self.cell.get_or_init(|| compile(self.pat, self.ci, self.dotall))
    }
    fn may_match(&self, text: &str) -> bool {
        if self.need.is_empty() {
            return true;
        }
        let t = folded(text);
        self.need.iter().all(|any| any.iter().any(|lit| t.contains(lit)))
    }
}

/// Could a match need these literals? (The prefilter, for a pattern built at run time.)
pub fn may_contain(text: &str, need: Need) -> bool {
    let t = folded(text);
    need.iter().all(|any| any.iter().any(|lit| t.contains(lit)))
}

/// Lower-case ASCII, plus the non-ASCII characters Python's re.IGNORECASE
/// matches to an ASCII letter (İ ı -> i, ſ -> s, Kelvin K -> k).
fn folded(text: &str) -> std::borrow::Cow<'_, str> {
    if text.is_ascii() && !text.bytes().any(|b| b.is_ascii_uppercase()) {
        return std::borrow::Cow::Borrowed(text);
    }
    std::borrow::Cow::Owned(
        text.chars()
            .map(|c| match c {
                '\u{130}' | '\u{131}' => 'i',
                '\u{17f}' => 's',
                '\u{212a}' => 'k',
                c => c.to_ascii_lowercase(),
            })
            .collect(),
    )
}

const PY_SPACE: &str = r"\s\x1c-\x1f"; // Python's \s is str.isspace(): U+001C..U+001F too

/// Python `re` syntax -> fancy-regex syntax with the same meaning:
///   `$` (no MULTILINE) also matches just before a final newline;
///   `\s` / `\S` follow str.isspace();
///   under IGNORECASE a literal i or I also matches İ and ı.
/// (`(\s|$)` keeps Rust's `$`: a final newline is matched by the `\s` branch.)
fn translate(pat: &str, ci: bool) -> String {
    let mut out = String::with_capacity(pat.len() + 32);
    let chars: Vec<char> = pat.chars().collect();
    let mut i = 0;
    while i < chars.len() {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() {
            match chars[i + 1] {
                's' => out.push_str(&format!("[{PY_SPACE}]")),
                'S' => out.push_str(&format!("[^{PY_SPACE}]")),
                n => {
                    out.push('\\');
                    out.push(n);
                }
            }
            i += 2;
            continue;
        }
        if c == '(' && chars[i..].starts_with(&['(', '\\', 's', '|', '$', ')']) {
            out.push_str(&format!("([{PY_SPACE}]|$)"));
            i += 6;
            continue;
        }
        match c {
            '[' => i = class(&chars, i, ci, &mut out),
            '$' => {
                out.push_str(r"(?=\n?(?![\s\S]))");
                i += 1;
            }
            'i' | 'I' if ci => {
                out.push_str("[iI\u{131}\u{130}]");
                i += 1;
            }
            _ => {
                out.push(c);
                i += 1;
            }
        }
    }
    out
}

/// Copy the character class starting at chars[start] ('['); returns the index
/// after its closing ']'.
fn class(chars: &[char], start: usize, ci: bool, out: &mut String) -> usize {
    let mut i = start + 1;
    let negated = chars.get(i) == Some(&'^');
    let mut body = String::new();
    let mut covers_i = false;
    if negated {
        i += 1;
    }
    let first = i;
    while i < chars.len() && (chars[i] != ']' || i == first) {
        let c = chars[i];
        if c == '\\' && i + 1 < chars.len() {
            match chars[i + 1] {
                's' => body.push_str(PY_SPACE),
                'S' => panic!("\\S inside a character class is not translated"),
                n => {
                    body.push('\\');
                    body.push(n);
                }
            }
            i += 2;
            continue;
        }
        if i + 2 < chars.len() && chars[i + 1] == '-' && chars[i + 2] != ']' {
            let (lo, hi) = (c, chars[i + 2]);
            covers_i |= (lo..=hi).contains(&'i') || (lo..=hi).contains(&'I');
            body.push(lo);
            body.push('-');
            body.push(hi);
            i += 3;
            continue;
        }
        covers_i |= c == 'i' || c == 'I';
        body.push(c);
        i += 1;
    }
    if ci && !negated && covers_i {
        body.push_str("\u{131}\u{130}");
    }
    out.push('[');
    if negated {
        out.push('^');
    }
    out.push_str(&body);
    out.push(']');
    i + 1
}

pub fn compile(pat: &str, ci: bool, dotall: bool) -> Regex {
    let mut s = String::new();
    if ci {
        s.push_str("(?i)");
    }
    if dotall {
        s.push_str("(?s)");
    }
    s.push_str(&translate(pat, ci));
    RegexBuilder::new(&s).build().unwrap_or_else(|e| panic!("pattern {pat:?}: {e}"))
}

fn rerr<T>(r: Result<T, fancy_regex::Error>) -> R<T> {
    r.map_err(|e| Bail(format!("regex engine: {e}")))
}

/// re.search(p, text) is not None, for a pattern built at run time.
pub fn search_re(re: &Regex, text: &str) -> R<bool> {
    rerr(re.is_match(text))
}

/// re.search(p, text) is not None
pub fn search(p: &L, text: &str) -> R<bool> {
    if !p.may_match(text) {
        return Ok(false);
    }
    rerr(p.get().is_match(text))
}

/// re.search(p, text): the captures of the first match.
pub fn captures<'t>(p: &L, text: &'t str) -> R<Option<Captures<'t, str>>> {
    if !p.may_match(text) {
        return Ok(None);
    }
    rerr(p.get().captures(text))
}

/// re.match(p, text) is not None (anchored at the start, not the end).
pub fn match_start(p: &L, text: &str) -> R<bool> {
    if !p.may_match(text) {
        return Ok(false);
    }
    Ok(rerr(p.get().find(text))?.is_some_and(|m| m.start() == 0))
}

/// re.finditer(p, text), collected.
pub fn find_all<'t>(p: &L, text: &'t str) -> R<Vec<Captures<'t, str>>> {
    let mut out = Vec::new();
    if !p.may_match(text) {
        return Ok(out);
    }
    for c in p.get().captures_iter(text) {
        out.push(rerr(c)?);
    }
    Ok(out)
}

/// re.sub(p, f, text)
pub fn sub(p: &L, text: &str, mut f: impl FnMut(&Captures<'_, str>) -> R<String>) -> R<String> {
    if !p.may_match(text) {
        return Ok(text.to_string());
    }
    let mut out = String::with_capacity(text.len());
    let mut last = 0;
    for c in p.get().captures_iter(text) {
        let c = rerr(c)?;
        let m = c.get(0).expect("group 0");
        out.push_str(&text[last..m.start()]);
        out.push_str(&f(&c)?);
        last = m.end();
    }
    out.push_str(&text[last..]);
    Ok(out)
}

/// m.group(i) or "" (callers only use groups that took part in the match).
pub fn group<'t>(c: &Captures<'t, str>, i: usize) -> &'t str {
    c.get(i).map_or("", |m| m.as_str())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dollar_matches_before_a_final_newline() {
        let re = compile(r"^a\.py$", false, false);
        assert!(re.is_match("a.py").unwrap());
        assert!(re.is_match("a.py\n").unwrap());
        assert!(!re.is_match("a.py\n\n").unwrap());
        let cls = compile(r"[$]x\$", false, false);
        assert!(cls.is_match("$x$").unwrap());
    }

    #[test]
    fn python_whitespace_and_dotted_i() {
        let re = compile(r"\bgit\s+push(\s|$)", true, false);
        for t in ["git push", "GIT\u{1c}PUSH\n", "g\u{130}t push x", "g\u{131}t\u{3000}push"] {
            assert!(re.is_match(t).unwrap(), "{t:?}");
        }
        assert!(!re.is_match("gxt push").unwrap());
        let cls = compile(r"-[a-zA-Z]*[rR][^;&|\s]", true, false);
        assert!(cls.is_match("-\u{131}r\u{1d}").is_ok_and(|m| !m));
        assert!(cls.is_match("-\u{131}rx").unwrap());
        assert_eq!(translate(r"[^\s;&|]+", false), "[^\\s\\x1c-\\x1f;&|]+");
    }

    #[test]
    fn prefilter_folds_like_ignorecase() {
        let p = L::ci_need(r"\bkubectl\s+delete\b", &[&["kubectl"], &["delete"]]);
        assert!(search(&p, "\u{212a}UBECTL DELETE pod").unwrap());
        assert!(!search(&p, "kubectl get pods").unwrap());
    }
}
