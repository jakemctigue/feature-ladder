//! JSON with the semantics of Python's `json` module, so the Rust guard reads
//! and rewrites run.json byte for byte as guard_irreversible.py would:
//!
//!   loads()          json.loads (strict): NaN/Infinity accepted, duplicate keys keep
//!                    the first position and the last value, ints kept exactly
//!   dumps_indent()   json.dumps(v, indent=2)  (ensure_ascii, ',' + newline, ': ')
//!   dumps_compact()  json.dumps(v)            (ensure_ascii, ', ' and ': ')
//!
//! Anything Python would hold that Rust cannot represent the same way (a lone
//! surrogate in a string, nesting deep enough to hit Python's recursion limit)
//! is `Unsupported`: the caller hands the call to the Python guard instead.

#[derive(Clone, Debug, PartialEq)]
pub enum Json {
    Null,
    Bool(bool),
    /// An integer, as its decimal digits (Python ints are unbounded).
    Int(String),
    Float(f64),
    Str(String),
    Arr(Vec<Json>),
    /// Insertion-ordered, unique keys (a Python dict).
    Obj(Vec<(String, Json)>),
}

#[derive(Debug, PartialEq)]
pub enum ParseError {
    /// Python raises ValueError (JSONDecodeError or the int digit limit).
    Invalid,
    /// Python would accept it but Rust cannot mirror the result exactly.
    Unsupported(&'static str),
}

pub fn s(v: &str) -> Json {
    Json::Str(v.to_string())
}

impl Json {
    pub fn get(&self, key: &str) -> Option<&Json> {
        match self {
            Json::Obj(m) => m.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    pub fn as_str(&self) -> Option<&str> {
        match self {
            Json::Str(s) => Some(s),
            _ => None,
        }
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Json::Null => false,
            Json::Bool(b) => *b,
            Json::Int(d) => d != "0",
            Json::Float(f) => *f != 0.0,
            Json::Str(s) => !s.is_empty(),
            Json::Arr(a) => !a.is_empty(),
            Json::Obj(o) => !o.is_empty(),
        }
    }
}

// ------------------------------------------------------------------ parsing --

const MAX_DEPTH: usize = 400; // well inside CPython's default recursion limit
const MAX_INT_DIGITS: usize = 4300; // sys.int_info.default_max_str_digits

struct Parser<'a> {
    b: &'a [u8],
    s: &'a str,
    i: usize,
    depth: usize,
}

pub fn loads(text: &str) -> Result<Json, ParseError> {
    let mut p = Parser { b: text.as_bytes(), s: text, i: 0, depth: 0 };
    p.ws();
    let v = p.value()?;
    p.ws();
    if p.i != p.b.len() {
        return Err(ParseError::Invalid); // "Extra data"
    }
    Ok(v)
}

impl<'a> Parser<'a> {
    fn ws(&mut self) {
        while self.i < self.b.len() && matches!(self.b[self.i], b' ' | b'\t' | b'\n' | b'\r') {
            self.i += 1;
        }
    }

    fn peek(&self) -> Option<u8> {
        self.b.get(self.i).copied()
    }

    fn lit(&mut self, word: &str, v: Json) -> Result<Json, ParseError> {
        if self.s[self.i..].starts_with(word) {
            self.i += word.len();
            Ok(v)
        } else {
            Err(ParseError::Invalid)
        }
    }

    fn value(&mut self) -> Result<Json, ParseError> {
        match self.peek() {
            Some(b'{') => self.object(),
            Some(b'[') => self.array(),
            Some(b'"') => {
                self.i += 1;
                Ok(Json::Str(self.string()?))
            }
            Some(b'n') => self.lit("null", Json::Null),
            Some(b't') => self.lit("true", Json::Bool(true)),
            Some(b'f') => self.lit("false", Json::Bool(false)),
            Some(b'N') => self.lit("NaN", Json::Float(f64::NAN)),
            Some(b'I') => self.lit("Infinity", Json::Float(f64::INFINITY)),
            Some(b'-') | Some(b'0'..=b'9') => {
                if let Some(v) = self.number()? {
                    Ok(v)
                } else if self.s[self.i..].starts_with("-Infinity") {
                    self.i += 9;
                    Ok(Json::Float(f64::NEG_INFINITY))
                } else {
                    Err(ParseError::Invalid)
                }
            }
            _ => Err(ParseError::Invalid),
        }
    }

    fn enter(&mut self) -> Result<(), ParseError> {
        self.depth += 1;
        if self.depth > MAX_DEPTH {
            return Err(ParseError::Unsupported("deeply nested JSON"));
        }
        Ok(())
    }

    fn object(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.i += 1;
        let mut out: Vec<(String, Json)> = Vec::new();
        self.ws();
        if self.peek() == Some(b'}') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Obj(out));
        }
        loop {
            if self.peek() != Some(b'"') {
                return Err(ParseError::Invalid);
            }
            self.i += 1;
            let key = self.string()?;
            self.ws();
            if self.peek() != Some(b':') {
                return Err(ParseError::Invalid);
            }
            self.i += 1;
            self.ws();
            let v = self.value()?;
            if let Some(slot) = out.iter_mut().find(|(k, _)| *k == key) {
                slot.1 = v; // dict: first position, last value
            } else {
                out.push((key, v));
            }
            self.ws();
            match self.peek() {
                Some(b'}') => {
                    self.i += 1;
                    break;
                }
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                _ => return Err(ParseError::Invalid),
            }
        }
        self.depth -= 1;
        Ok(Json::Obj(out))
    }

    fn array(&mut self) -> Result<Json, ParseError> {
        self.enter()?;
        self.i += 1;
        let mut out = Vec::new();
        self.ws();
        if self.peek() == Some(b']') {
            self.i += 1;
            self.depth -= 1;
            return Ok(Json::Arr(out));
        }
        loop {
            out.push(self.value()?);
            self.ws();
            match self.peek() {
                Some(b']') => {
                    self.i += 1;
                    break;
                }
                Some(b',') => {
                    self.i += 1;
                    self.ws();
                }
                _ => return Err(ParseError::Invalid),
            }
        }
        self.depth -= 1;
        Ok(Json::Arr(out))
    }

    /// Python's number scanner; Ok(None) when no number starts here.
    fn number(&mut self) -> Result<Option<Json>, ParseError> {
        let b = self.b;
        let start = self.i;
        let mut j = self.i;
        if b.get(j) == Some(&b'-') {
            j += 1;
        }
        match b.get(j) {
            Some(b'0') => j += 1,
            Some(b'1'..=b'9') => {
                while matches!(b.get(j), Some(b'0'..=b'9')) {
                    j += 1;
                }
            }
            _ => return Ok(None),
        }
        let mut is_float = false;
        if b.get(j) == Some(&b'.') && matches!(b.get(j + 1), Some(b'0'..=b'9')) {
            is_float = true;
            j += 1;
            while matches!(b.get(j), Some(b'0'..=b'9')) {
                j += 1;
            }
        }
        if matches!(b.get(j), Some(b'e') | Some(b'E')) {
            let mut k = j + 1;
            if matches!(b.get(k), Some(b'+') | Some(b'-')) {
                k += 1;
            }
            if matches!(b.get(k), Some(b'0'..=b'9')) {
                while matches!(b.get(k), Some(b'0'..=b'9')) {
                    k += 1;
                }
                is_float = true;
                j = k;
            }
        }
        self.i = j;
        let text = &self.s[start..j];
        if is_float {
            return text.parse::<f64>().map(|f| Some(Json::Float(f))).map_err(|_| ParseError::Invalid);
        }
        let (neg, digits) = match text.strip_prefix('-') {
            Some(d) => (true, d),
            None => (false, text),
        };
        if digits.len() > MAX_INT_DIGITS {
            return Err(ParseError::Invalid); // ValueError: exceeds the int digit limit
        }
        Ok(Some(Json::Int(if neg && digits != "0" { text.to_string() } else { digits.to_string() })))
    }

    fn hex4(&self, at: usize) -> Option<u32> {
        let h = self.s.get(at..at + 4)?;
        if h.bytes().all(|c| c.is_ascii_hexdigit()) {
            u32::from_str_radix(h, 16).ok()
        } else {
            None
        }
    }

    /// After the opening quote; strict=True (no raw control characters).
    fn string(&mut self) -> Result<String, ParseError> {
        let mut out = String::new();
        loop {
            let rest = &self.s[self.i..];
            let mut chars = rest.char_indices();
            let Some((_, ch)) = chars.next() else {
                return Err(ParseError::Invalid); // unterminated
            };
            self.i += ch.len_utf8();
            match ch {
                '"' => return Ok(out),
                '\\' => {
                    let Some(e) = self.peek() else { return Err(ParseError::Invalid) };
                    self.i += 1;
                    match e {
                        b'"' => out.push('"'),
                        b'\\' => out.push('\\'),
                        b'/' => out.push('/'),
                        b'b' => out.push('\u{8}'),
                        b'f' => out.push('\u{c}'),
                        b'n' => out.push('\n'),
                        b'r' => out.push('\r'),
                        b't' => out.push('\t'),
                        b'u' => {
                            let Some(c) = self.hex4(self.i) else { return Err(ParseError::Invalid) };
                            self.i += 4;
                            if (0xd800..=0xdbff).contains(&c) {
                                if self.s[self.i..].starts_with("\\u") {
                                    if let Some(lo) = self.hex4(self.i + 2) {
                                        if (0xdc00..=0xdfff).contains(&lo) {
                                            self.i += 6;
                                            let cp = 0x10000 + ((c - 0xd800) << 10) + (lo - 0xdc00);
                                            out.push(char::from_u32(cp).ok_or(ParseError::Invalid)?);
                                            continue;
                                        }
                                    } else {
                                        return Err(ParseError::Invalid);
                                    }
                                }
                                return Err(ParseError::Unsupported("lone surrogate in a JSON string"));
                            }
                            if (0xdc00..=0xdfff).contains(&c) {
                                return Err(ParseError::Unsupported("lone surrogate in a JSON string"));
                            }
                            out.push(char::from_u32(c).ok_or(ParseError::Invalid)?);
                        }
                        _ => return Err(ParseError::Invalid),
                    }
                }
                c if (c as u32) < 0x20 => return Err(ParseError::Invalid),
                c => out.push(c),
            }
        }
    }
}

// ------------------------------------------------------------------ writing --

/// repr(float) for a finite value: the shortest round-trip digits, positional
/// for 1e-4 <= |x| < 1e16, otherwise 'd.ddde+XX'.
pub fn float_repr(x: f64) -> String {
    if x.is_nan() {
        return "NaN".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "Infinity".into() } else { "-Infinity".into() };
    }
    let e = format!("{:e}", x); // shortest round-trip, e.g. "-1.2345e-5", "0e0"
    let (neg, e) = match e.strip_prefix('-') {
        Some(r) => (true, r),
        None => (false, e.as_str()),
    };
    let (mant, exp) = e.split_once('e').expect("LowerExp has an exponent");
    let exp: i32 = exp.parse().expect("exponent");
    let digits: String = mant.chars().filter(|c| *c != '.').collect();
    let decpt = exp + 1; // digits d1d2... represent 0.d1d2... * 10^decpt
    let mut out = String::new();
    if neg {
        out.push('-');
    }
    if decpt <= -4 || decpt > 16 {
        out.push_str(&digits[..1]);
        if digits.len() > 1 {
            out.push('.');
            out.push_str(&digits[1..]);
        }
        let x = decpt - 1;
        out.push('e');
        out.push(if x < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", x.abs()));
    } else if decpt <= 0 {
        out.push_str("0.");
        out.push_str(&"0".repeat((-decpt) as usize));
        out.push_str(&digits);
    } else if decpt as usize >= digits.len() {
        out.push_str(&digits);
        out.push_str(&"0".repeat(decpt as usize - digits.len()));
        out.push_str(".0");
    } else {
        out.push_str(&digits[..decpt as usize]);
        out.push('.');
        out.push_str(&digits[decpt as usize..]);
    }
    out
}

/// A JSON string literal with ensure_ascii=True.
pub fn encode_str(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            ' '..='~' => out.push(c),
            _ => {
                let mut buf = [0u16; 2];
                for u in c.encode_utf16(&mut buf) {
                    out.push_str(&format!("\\u{:04x}", u));
                }
            }
        }
    }
    out.push('"');
}

fn write(v: &Json, indent: Option<usize>, level: usize, out: &mut String) {
    match v {
        Json::Null => out.push_str("null"),
        Json::Bool(true) => out.push_str("true"),
        Json::Bool(false) => out.push_str("false"),
        Json::Int(d) => out.push_str(d),
        Json::Float(f) => out.push_str(&float_repr(*f)),
        Json::Str(s) => encode_str(s, out),
        Json::Arr(a) if a.is_empty() => out.push_str("[]"),
        Json::Obj(o) if o.is_empty() => out.push_str("{}"),
        Json::Arr(a) => {
            out.push('[');
            for (n, item) in a.iter().enumerate() {
                sep(n, indent, level + 1, out);
                write(item, indent, level + 1, out);
            }
            close(indent, level, out);
            out.push(']');
        }
        Json::Obj(o) => {
            out.push('{');
            for (n, (k, item)) in o.iter().enumerate() {
                sep(n, indent, level + 1, out);
                encode_str(k, out);
                out.push_str(": ");
                write(item, indent, level + 1, out);
            }
            close(indent, level, out);
            out.push('}');
        }
    }
}

fn sep(n: usize, indent: Option<usize>, level: usize, out: &mut String) {
    match indent {
        Some(w) => {
            if n > 0 {
                out.push(',');
            }
            out.push('\n');
            out.push_str(&" ".repeat(w * level));
        }
        None => {
            if n > 0 {
                out.push_str(", ");
            }
        }
    }
}

fn close(indent: Option<usize>, level: usize, out: &mut String) {
    if let Some(w) = indent {
        out.push('\n');
        out.push_str(&" ".repeat(w * level));
    }
}

pub fn dumps_indent(v: &Json, indent: usize) -> String {
    let mut out = String::new();
    write(v, Some(indent), 0, &mut out);
    out
}

pub fn dumps_compact(v: &Json) -> String {
    let mut out = String::new();
    write(v, None, 0, &mut out);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn float_repr_matches_python() {
        let cases = [
            (0.0, "0.0"), (-0.0, "-0.0"), (1.0, "1.0"), (1.5, "1.5"), (0.1, "0.1"), (1e16, "1e+16"),
            (1e15, "1000000000000000.0"), (123456789012345678.0, "1.2345678901234568e+17"), (0.0001, "0.0001"),
            (0.00001, "1e-05"), (1.5e-7, "1.5e-07"), (1732400000.123456, "1732400000.123456"), (1e300, "1e+300"),
            (2.5e-300, "2.5e-300"), (-12.75, "-12.75"), (100.0, "100.0"),
        ];
        for (x, want) in cases {
            assert_eq!(float_repr(x), want, "{x}");
        }
    }

    #[test]
    fn round_trips_like_python() {
        let v = loads(r#"{"a": 1, "b": [1.50, -0, "é😀\u0001"], "a": {"x": NaN}, "c": {}, "d": []}"#).unwrap();
        assert_eq!(
            dumps_indent(&v, 2),
            "{\n  \"a\": {\n    \"x\": NaN\n  },\n  \"b\": [\n    1.5,\n    0,\n    \"\\u00e9\\ud83d\\ude00\\u0001\"\n  ],\n  \"c\": {},\n  \"d\": []\n}"
        );
        assert_eq!(dumps_compact(&loads(r#"{"k": [1, "\u007f"]}"#).unwrap()), r#"{"k": [1, "\u007f"]}"#);
    }

    #[test]
    fn rejects_what_python_rejects() {
        for bad in ["", "{", "[1,]", "{\"a\":1,}", "01", "1.", "\"\t\"", "[1] x", "{'a': 1}", "\"\\x\""] {
            assert_eq!(loads(bad), Err(ParseError::Invalid), "{bad:?}");
        }
        assert!(matches!(loads("\"\\ud800\""), Err(ParseError::Unsupported(_))));
    }
}
