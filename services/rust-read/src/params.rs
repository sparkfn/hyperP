use std::collections::HashMap;

/// Parse and validate a query explicitly so extractors never emit plain-text errors.
pub struct Params(HashMap<String, Vec<String>>);

impl Params {
    pub fn parse(raw: Option<&str>, allowed: &[&str], repeated: &[&str]) -> Result<Self, ()> {
        let mut values: HashMap<String, Vec<String>> = HashMap::new();
        for (key, value) in url::form_urlencoded::parse(raw.unwrap_or("").as_bytes()) {
            if !allowed.contains(&key.as_ref()) {
                return Err(());
            }
            let may_repeat = repeated.contains(&key.as_ref());
            let entry = values.entry(key.into_owned()).or_default();
            if !entry.is_empty() && !may_repeat {
                return Err(());
            }
            entry.push(value.into_owned());
        }
        Ok(Self(values))
    }

    pub fn get(&self, key: &str) -> Option<&str> {
        self.0
            .get(key)
            .and_then(|values| values.first())
            .map(String::as_str)
    }

    pub fn list(&self, key: &str) -> &[String] {
        self.0.get(key).map(Vec::as_slice).unwrap_or_default()
    }

    pub fn boolean(&self, key: &str) -> Result<Option<bool>, ()> {
        match self.get(key).map(str::to_ascii_lowercase).as_deref() {
            None => Ok(None),
            Some("true" | "t" | "1" | "on" | "yes" | "y") => Ok(Some(true)),
            Some("false" | "f" | "0" | "off" | "no" | "n") => Ok(Some(false)),
            _ => Err(()),
        }
    }

    pub fn integer(&self, key: &str) -> Result<Option<i64>, ()> {
        self.get(key)
            .map(|value| value.parse::<i64>().map_err(|_| ()))
            .transpose()
    }
}
