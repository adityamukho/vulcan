use axum::{
    extract::Json,
    http::StatusCode,
    routing::{get, post},
    Router,
};
use serde::{Deserialize, Serialize};
use std::process::Command;
use std::sync::OnceLock;

static GRAPH_PATH: OnceLock<String> = OnceLock::new();

fn get_graph_path() -> String {
    GRAPH_PATH
        .get_or_init(|| {
            std::env::var("MINIGRAF_GRAPH_PATH").unwrap_or_else(|_| {
                if std::path::Path::new("/tmp").map(|p| p.exists()).unwrap_or(false) {
                    "/tmp/minigraf_memory.graph".to_string()
                } else {
                    let home = std::env::var("HOME").unwrap_or_else(|_| "/tmp".to_string());
                    std::path::Path::new(&home)
                        .join(".local")
                        .join("share")
                        .join("temporal-reasoning")
                        .join("memory.graph")
                        .to_string_lossy()
                        .to_string()
                }
            })
        })
        .clone()
}

fn get_minigraf_path() -> String {
    std::env::var("MINIGRAF_BIN").unwrap_or_else(|_| "minigraf".to_string())
}

#[derive(Debug, Serialize, Deserialize)]
pub struct QueryRequest {
    pub datalog: String,
    #[serde(default)]
    pub as_of: Option<String>,
}

#[derive(Debug, Serialize, Deserialize)]
pub struct TransactRequest {
    pub facts: String,
    #[serde(default)]
    pub reason: Option<String>,
}

#[derive(Debug, Serialize)]
pub struct HealthResponse {
    pub ok: bool,
}

#[derive(Debug, Serialize)]
pub struct ApiResponse<T> {
    #[serde(flatten)]
    pub inner: T,
}

async fn query_handler(Json(payload): Json<QueryRequest>) -> Result<Json<serde_json::Value>, StatusCode> {
    let path = get_graph_path();
    let minigraf = get_minigraf_path();

    if !std::path::Path::new(&path).exists() {
        return Err(StatusCode::NOT_FOUND);
    }

    let mut datalog = payload.datalog;
    if let Some(as_of) = &payload.as_of {
        if !datalog.contains(":as-of") {
            if datalog.contains(":find") {
                datalog = datalog.replacen(":find", &format!(":find :as-of {} ", as_of), 1);
            } else {
                datalog = format!("[:as-of {} {}]", as_of, datalog);
            }
        }
    }

    let input = format!("(query {})", datalog);

    let output = Command::new(&minigraf)
        .args(["--file", &path])
        .input(input)
        .output()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    if !output.status.success() {
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    
    if stdout.contains("No results found") {
        return Ok(Json(serde_json::json!({"ok": true, "results": []})));
    }

    let lines: Vec<&str> = stdout.lines().skip(2).filter(|l| !l.starts_with("---") && !l.is_empty()).collect();
    let results: Vec<Vec<String>> = lines
        .iter()
        .map(|l| l.split('|').map(|v| v.trim().to_string()).collect())
        .collect();

    Ok(Json(serde_json::json!({"ok": true, "results": results})))
}

async fn transact_handler(Json(payload): Json<TransactRequest>) -> Result<Json<serde_json::Value>, StatusCode> {
    let path = get_graph_path();
    let minigraf = get_minigraf_path();

    let input = format!("(transact {})", payload.facts);

    let output = Command::new(&minigraf)
        .args(["--file", &path])
        .input(input)
        .output()
        .map_err(|_| StatusCode::INTERNAL_SERVER_ERROR)?;

    if !output.status.success() {
        return Err(StatusCode::INTERNAL_SERVER_ERROR);
    }

    let stdout = String::from_utf8_lossy(&output.stdout);
    let tx = if stdout.contains("tx:") {
        stdout.split("tx:").nth(1).unwrap_or("unknown").trim().trim_end_matches(')').to_string()
    } else {
        "unknown".to_string()
    };

    Ok(Json(serde_json::json!({"ok": true, "tx": tx, "reason": payload.reason})))
}

async fn health_handler() -> Json<HealthResponse> {
    Json(HealthResponse { ok: true })
}

#[tokio::main]
async fn main() {
    let app = Router::new()
        .route("/health", get(health_handler))
        .route("/query", post(query_handler))
        .route("/transact", post(transact_handler));

    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    println!("Server running on http://127.0.0.1:3000");
    axum::serve(listener, app).await.unwrap();
}