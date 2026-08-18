use axum::{
    body::Body,
    http::{Request, StatusCode},
};
use http_body_util::BodyExt;
use memory_bread_core::{
    api::{create_router, AppState},
    storage::StorageManager,
};
use tower::ServiceExt;

#[tokio::test]
async fn data_sources_empty_db_returns_a_page() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("test.db")).unwrap();
    let router = create_router(AppState::new(storage));
    let request = Request::builder()
        .uri("/api/data/sources?limit=20&offset=0")
        .body(Body::empty())
        .unwrap();

    let response = router.oneshot(request).await.unwrap();
    assert_eq!(response.status(), StatusCode::OK);

    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["items"], serde_json::json!([]));
    assert_eq!(json["total"], 0);
    assert_eq!(json["pending_items"], serde_json::json!([]));
    assert_eq!(json["pending_total"], 0);
    assert_eq!(json["limit"], 20);
    assert_eq!(json["offset"], 0);
}

#[tokio::test]
async fn manual_data_supports_create_and_update() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("test.db")).unwrap();
    let router = create_router(AppState::new(storage));

    let created = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/data/sources")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "title":"本周经营指标",
                        "summary":"本周订单 1200",
                        "rows":[
                            {"dimension":"本周","metric":"订单","value":"1200","note":"环比增长 8%"}
                        ]
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(created.status(), StatusCode::OK);
    let created_body = created.into_body().collect().await.unwrap().to_bytes();
    let created_json: serde_json::Value = serde_json::from_slice(&created_body).unwrap();
    let source_id = created_json["id"].as_i64().unwrap();
    assert_eq!(
        created_json["latest_snapshot"]["structured_data"]["title"],
        "本周经营指标"
    );
    assert_eq!(
        created_json["latest_snapshot"]["structured_data"]["manual_entry"],
        true
    );

    let updated = router
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("/api/data/sources/{source_id}"))
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "title":"本周经营数据",
                        "summary":"本周订单 1320",
                        "rows":[
                            {"dimension":"本周","metric":"订单","value":"1320","note":"已复核"},
                            {"dimension":"本周","metric":"退款率","value":"1.2%","note":""}
                        ]
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(updated.status(), StatusCode::OK);
    let updated_body = updated.into_body().collect().await.unwrap().to_bytes();
    let updated_json: serde_json::Value = serde_json::from_slice(&updated_body).unwrap();
    assert_eq!(updated_json["title"], "本周经营数据");
    assert_eq!(
        updated_json["latest_snapshot"]["structured_data"]["summary"],
        "本周订单 1320"
    );
    assert_eq!(
        updated_json["latest_snapshot"]["structured_data"]["metric_rows"]
            .as_array()
            .unwrap()
            .len(),
        2
    );

    let listed = router
        .oneshot(
            Request::builder()
                .uri("/api/data/sources?limit=20&offset=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(listed.status(), StatusCode::OK);
    let listed_body = listed.into_body().collect().await.unwrap().to_bytes();
    let listed_json: serde_json::Value = serde_json::from_slice(&listed_body).unwrap();
    assert_eq!(listed_json["total"], 1);
    assert_eq!(
        listed_json["items"][0]["latest_snapshot"]["structured_data"]["title"],
        "本周经营数据"
    );
}

#[tokio::test]
async fn data_favorites_are_returned_by_detail_and_filter_both_list_states() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("test.db")).unwrap();
    let router = create_router(AppState::new(storage));

    let created = router
        .clone()
        .oneshot(
            Request::builder()
                .method("POST")
                .uri("/api/data/sources")
                .header("content-type", "application/json")
                .body(Body::from(
                    r#"{
                        "title":"收藏数据",
                        "summary":"收藏筛选测试",
                        "rows":[{"metric":"订单","value":"1"}]
                    }"#,
                ))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(created.status(), StatusCode::OK);
    let created_body = created.into_body().collect().await.unwrap().to_bytes();
    let created_json: serde_json::Value = serde_json::from_slice(&created_body).unwrap();
    let favorite_id = created_json["id"].as_i64().unwrap();

    let initially_not_favorite = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/data/sources?favorite=false&limit=20&offset=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let initially_not_favorite_body = initially_not_favorite
        .into_body()
        .collect()
        .await
        .unwrap()
        .to_bytes();
    let initially_not_favorite_json: serde_json::Value =
        serde_json::from_slice(&initially_not_favorite_body).unwrap();
    assert_eq!(initially_not_favorite_json["total"], 1);
    assert_eq!(initially_not_favorite_json["items"][0]["id"], favorite_id);
    assert_eq!(
        initially_not_favorite_json["items"][0]["is_favorite"],
        false
    );

    let favorite_response = router
        .clone()
        .oneshot(
            Request::builder()
                .method("PUT")
                .uri(format!("/api/memory-favorites/data/{favorite_id}"))
                .header("content-type", "application/json")
                .body(Body::from(r#"{"is_favorite":true}"#))
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(favorite_response.status(), StatusCode::OK);

    let detail = router
        .clone()
        .oneshot(
            Request::builder()
                .uri(format!("/api/data/sources/{favorite_id}"))
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(detail.status(), StatusCode::OK);
    let detail_body = detail.into_body().collect().await.unwrap().to_bytes();
    let detail_json: serde_json::Value = serde_json::from_slice(&detail_body).unwrap();
    assert_eq!(detail_json["is_favorite"], true);

    for (favorite, expected_total) in [(true, 1), (false, 0)] {
        let response = router
            .clone()
            .oneshot(
                Request::builder()
                    .uri(format!(
                        "/api/data/sources?favorite={favorite}&limit=20&offset=0"
                    ))
                    .body(Body::empty())
                    .unwrap(),
            )
            .await
            .unwrap();
        assert_eq!(response.status(), StatusCode::OK);
        let body = response.into_body().collect().await.unwrap().to_bytes();
        let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
        assert_eq!(
            json["total"], expected_total,
            "favorite={favorite}, body={json}"
        );
        if expected_total == 1 {
            assert_eq!(json["items"][0]["id"], favorite_id);
            assert_eq!(json["items"][0]["is_favorite"], favorite);
        }
    }
}

#[tokio::test]
async fn browser_preview_endpoint_rejects_invalid_ids_before_reading_disk() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("test.db")).unwrap();
    let router = create_router(AppState::new(storage));

    let invalid = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/creation/browser-previews/not-a-uuid/image")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(invalid.status(), StatusCode::BAD_REQUEST);

    let missing = router
        .oneshot(
            Request::builder()
                .uri("/api/creation/browser-previews/2d870d80-e2a2-4424-a732-069e174f2796/image")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(missing.status(), StatusCode::NOT_FOUND);
}

#[tokio::test]
async fn data_page_counts_only_semantic_records_and_supports_delete() {
    let temp_dir = tempfile::tempdir().unwrap();
    let storage = StorageManager::open(&temp_dir.path().join("test.db")).unwrap();
    storage
        .with_conn(|conn| {
            conn.execute_batch(
                r#"
                INSERT INTO data_sources (
                    id, canonical_key, title, source_kind, access_mode, refresh_policy,
                    realtime_level, tags, first_seen_at, last_seen_at, status, created_at, updated_at
                ) VALUES
                    (1, 'memory:timeline:71', 'GPU 数据', 'work_memory', 'memory_only', 'never',
                     'observed', '["work_memory"]', 1, 3, 'active', 1, 3),
                    (2, 'memory:timeline:72', '孤立数字', 'work_memory', 'memory_only', 'never',
                     'observed', '["work_memory"]', 1, 2, 'active', 1, 2),
                    (3, 'report:https://bi.example.com/dashboard', '经营看板', 'report_url',
                     'browser_session', 'on_demand', 'live', '["report"]', 1, 1, 'active', 1, 1);

                INSERT INTO data_snapshots (
                    source_id, collected_at, observed_at, collector, content_text,
                    structured_data, content_hash, freshness_ttl_seconds, provenance,
                    source_capture_ids, source_timeline_ids, status, created_at
                ) VALUES
                    (1, 3, 3, 'memory_extract',
                     '背景显示国内日均 GPU 利用率为 42%，海外为 47%',
                     '{"metric_statements":[{"statement":"背景显示国内日均 GPU 利用率为 42%，海外为 47%","observed_at":3}]}',
                     'gpu', 0, '{}', '[]', '[71]', 'success', 3),
                    (2, 2, 2, 'memory_extract', '9类 43%',
                     '{"metric_statements":[{"statement":"9类 43%","observed_at":2}]}',
                     'orphan', 0, '{}', '[]', '[72]', 'success', 2);
                "#,
            )?;
            Ok(())
        })
        .unwrap();
    let router = create_router(AppState::new(storage));

    let response = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/data/sources?limit=1&offset=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(response.status(), StatusCode::OK);
    let body = response.into_body().collect().await.unwrap().to_bytes();
    let json: serde_json::Value = serde_json::from_slice(&body).unwrap();
    assert_eq!(json["total"], 1);
    assert_eq!(json["items"].as_array().unwrap().len(), 1);
    assert_eq!(json["items"][0]["id"], 1);
    assert_eq!(
        json["items"][0]["latest_snapshot"]["structured_data"]["extraction_version"],
        "data-memory.v16"
    );
    assert_eq!(
        json["items"][0]["latest_snapshot"]["structured_data"]["title"],
        "GPU 利用率对比"
    );
    assert_eq!(
        json["items"][0]["latest_snapshot"]["structured_data"]["metric_rows"]
            .as_array()
            .unwrap()
            .len(),
        2
    );
    assert_eq!(json["pending_total"], 1);
    assert_eq!(json["pending_items"][0]["id"], 3);

    let second_page = router
        .clone()
        .oneshot(
            Request::builder()
                .uri("/api/data/sources?limit=1&offset=1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let second_body = second_page.into_body().collect().await.unwrap().to_bytes();
    let second_json: serde_json::Value = serde_json::from_slice(&second_body).unwrap();
    assert_eq!(second_json["total"], 1);
    assert!(second_json["items"].as_array().unwrap().is_empty());

    let deleted = router
        .clone()
        .oneshot(
            Request::builder()
                .method("DELETE")
                .uri("/api/data/sources/1")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    assert_eq!(deleted.status(), StatusCode::NO_CONTENT);

    let after_delete = router
        .oneshot(
            Request::builder()
                .uri("/api/data/sources?limit=20&offset=0")
                .body(Body::empty())
                .unwrap(),
        )
        .await
        .unwrap();
    let after_delete_body = after_delete.into_body().collect().await.unwrap().to_bytes();
    let after_delete_json: serde_json::Value = serde_json::from_slice(&after_delete_body).unwrap();
    assert_eq!(after_delete_json["total"], 0);
}
