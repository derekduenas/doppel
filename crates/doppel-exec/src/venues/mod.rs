pub mod kalshi;
pub mod pmus;

pub struct Request {
    pub method: &'static str,
    pub path: String,
    pub body: Option<serde_json::Value>,
    pub headers: Vec<(String, String)>,
}
