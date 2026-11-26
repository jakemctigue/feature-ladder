//! Python's shlex.shlex(s, posix=True, punctuation_chars="()<>") with
//! whitespace_split=True and commenters="", iterated to the end: the state
//! machine of shlex.read_token, transcribed. Err(()) is the ValueError Python
//! raises for an unclosed quote or a trailing backslash.

#[derive(Clone, Copy, PartialEq)]
enum St {
    Space,
    Word,
    Punct,
    Quote(char),
    Escape,
    Eof,
}

fn ws(c: char) -> bool {
    matches!(c, ' ' | '\t' | '\r' | '\n')
}

fn punct(c: char) -> bool {
    matches!(c, '(' | ')' | '<' | '>')
}

fn quote(c: char) -> bool {
    c == '\'' || c == '"'
}

pub fn tokens(s: &str) -> Result<Vec<String>, ()> {
    let chars: Vec<char> = s.chars().collect();
    let mut pos = 0usize;
    let mut pushback: Option<char> = None;
    let mut state = St::Space;
    let mut out = Vec::new();
    loop {
        // one read_token()
        let mut quoted = false;
        let mut escaped_state = St::Space;
        let mut token = String::new();
        loop {
            let next = if let Some(c) = pushback.take() {
                Some(c)
            } else if pos < chars.len() {
                pos += 1;
                Some(chars[pos - 1])
            } else {
                None
            };
            match state {
                St::Eof => {
                    token.clear();
                    break;
                }
                St::Space => match next {
                    None => {
                        state = St::Eof;
                        break;
                    }
                    Some(c) if ws(c) => {
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                    Some('\\') => {
                        escaped_state = St::Word;
                        state = St::Escape;
                    }
                    Some(c) if punct(c) => {
                        token.push(c);
                        state = St::Punct;
                    }
                    Some(c) if quote(c) => state = St::Quote(c),
                    Some(c) => {
                        token.push(c); // a word character, or any character under whitespace_split
                        state = St::Word;
                    }
                },
                St::Quote(q) => {
                    quoted = true;
                    match next {
                        None => return Err(()),
                        Some(c) if c == q => state = St::Word,
                        Some('\\') if q == '"' => {
                            escaped_state = St::Quote(q);
                            state = St::Escape;
                        }
                        Some(c) => token.push(c),
                    }
                }
                St::Escape => match next {
                    None => return Err(()),
                    Some(c) => {
                        if let St::Quote(eq) = escaped_state {
                            if c != '\\' && c != eq {
                                token.push('\\');
                            }
                        }
                        token.push(c);
                        state = escaped_state;
                    }
                },
                St::Word | St::Punct => match next {
                    None => {
                        state = St::Eof;
                        break;
                    }
                    Some(c) if ws(c) => {
                        state = St::Space;
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                    Some(c) if state == St::Punct => {
                        if punct(c) {
                            token.push(c);
                        } else {
                            pushback = Some(c); // whitespace was handled above
                            state = St::Space;
                            break;
                        }
                    }
                    Some(c) if quote(c) => state = St::Quote(c),
                    Some('\\') => {
                        escaped_state = St::Word;
                        state = St::Escape;
                    }
                    Some(c) if !punct(c) => token.push(c),
                    Some(c) => {
                        pushback = Some(c);
                        state = St::Space;
                        if !token.is_empty() || quoted {
                            break;
                        }
                    }
                },
            }
        }
        if !quoted && token.is_empty() {
            return Ok(out); // posix EOF
        }
        out.push(token);
    }
}

#[cfg(test)]
mod tests {
    use super::tokens;

    fn t(s: &str) -> Vec<String> {
        tokens(s).unwrap()
    }

    #[test]
    fn like_python() {
        assert_eq!(t("cat < .env"), ["cat", "<", ".env"]);
        assert_eq!(t("echo 'a b' \"c\\\"d\" e\\ f"), ["echo", "a b", "c\"d", "e f"]);
        assert_eq!(t("a 2>&1 >>out (x)"), ["a", "2", ">", "&1", ">>", "out", "(", "x", ")"]);
        assert_eq!(t("x=''"), ["x="]);
        assert_eq!(t("''"), [""]);
        assert_eq!(t("cat <<< \"$X_KEY\""), ["cat", "<<<", "$X_KEY"]);
        assert_eq!(t("\"a\\b\""), ["a\\b"]);
        assert!(tokens("echo 'open").is_err());
        assert!(tokens("echo \\").is_err());
    }
}
