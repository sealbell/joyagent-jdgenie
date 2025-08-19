package com.jd.genie.agent.tool.common;

import com.alibaba.fastjson.JSON;
import com.alibaba.fastjson.JSONObject;
import com.jd.genie.agent.agent.AgentContext;
import com.jd.genie.agent.dto.FileRequest;
import com.jd.genie.agent.tool.BaseTool;
import lombok.Data;
import lombok.extern.slf4j.Slf4j;
import okhttp3.*;

import java.io.File;
import java.io.FileWriter;
import java.lang.reflect.Method;
import java.nio.charset.StandardCharsets;
import java.util.*;
import java.util.concurrent.TimeUnit;

@Slf4j
@Data
public class CogneeMemoryTool implements BaseTool {

    private AgentContext agentContext;

    // 固定测试环境
    private static final String BASE_URL = "http://localhost:8000";
    private static final String EMAIL    = "admin@example.com";
    private static final String PASSWORD = "admin123";

    // 运行期认证（Cookie 优先，其次 Bearer）
    private String bearerToken = null;
    private Headers cookieHeaders = null;

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(60, TimeUnit.SECONDS)
            .writeTimeout(60, TimeUnit.SECONDS)
            .callTimeout(70, TimeUnit.SECONDS)
            .build();

    @Override
    public String getName() { return "cognee_memory"; }

    @Override
    public String getDescription() {
        return "凡是用户偏好/个人信息/习惯/称呼/日程/提醒/默认设置/常用工具相关问题，必须先调用本工具。本工具与 Cognee 记忆服务交互：鉴权 → add/cognify/search/visualize（含文件沉淀；cognify 默认后台/软失败）。";
    }

    @Override
    public Map<String, Object> toParams() {
        Map<String, Object> props = new LinkedHashMap<>();
        props.put("mode", Map.of("type","string","enum",List.of("add","search","visualize"),
                "description","add(写入记忆)、search(检索)、visualize(知识图谱HTML)"));
        props.put("text", Map.of("type","string","description","mode=add：写入的文本内容"));
        props.put("dataset_name", Map.of("type","string","description","缺省自动：jdgenie_profile 或 jdgenie_profile_<sessionId>"));
        props.put("dataset_id", Map.of("type","string","description","已有数据集ID（若提供则优先）"));
        props.put("do_cognify", Map.of("type","boolean","description","add 后是否 cognify（默认 false）"));
        props.put("run_in_background", Map.of("type","boolean","description","do_cognify=true 时是否后台（默认 true）"));
        props.put("query", Map.of("type","string","description","mode=search：检索文本"));
        props.put("search_type", Map.of("type","string","description","GRAPH_COMPLETION / SUMMARIES / CHUNKS / ...（默认 GRAPH_COMPLETION）"));
        props.put("top_k", Map.of("type","integer","description","TopK（默认 5）"));
        props.put("visualize_dataset_id", Map.of("type","string","description","mode=visualize：必填，数据集ID(UUID)"));

        Map<String, Object> params = new LinkedHashMap<>();
        params.put("type", "object");
        params.put("properties", props);
        params.put("required", List.of("mode"));
        return params;
    }

    @SuppressWarnings("unchecked")
    @Override
    public Object execute(Object input) {
        long t0 = System.currentTimeMillis();
        try {
            Map<String, Object> in = (Map<String, Object>) input;
            String mode = asStr(in.get("mode"));
            if (mode.isEmpty()) return err("mode is required");

            info("cognee: start (" + mode + ")");
            ensureAuth();
            info("cognee: auth ok");

            switch (mode) {
                case "add": {
                    String text = asStr(in.get("text"));
                    if (text.isEmpty()) return err("text is required for add");

                    String datasetId = resolveDataset(in);
                    info("cognee: dataset=" + datasetId);

                    String addResp = addText(datasetId, text);
                    info("cognee: add done");

                    boolean doCognify = Boolean.TRUE.equals(in.get("do_cognify"));            // 默认 false
                    boolean runInBackground = !Boolean.FALSE.equals(in.get("run_in_background")); // 默认 true

                    JSONObject out = new JSONObject();
                    out.put("op", "add");
                    out.put("status", "ok");
                    out.put("datasetId", datasetId);
                    out.put("add_response", parseJsonSafe(addResp));

                    if (doCognify) {
                        info("cognee: cognify running ...");
                        try {
                            JSONObject cognifyResp = cognify(
                                    List.of(datasetId),
                                    runInBackground,
                                    runInBackground ? null : 45
                            );
                            out.put("cognify_response", cognifyResp);
                            info("cognee: cognify done");
                        } catch (Exception ce) {
                            log.warn("{} cognify failed/timeout but add succeeded: {}", rid(), ce.toString());
                            out.put("cognify_error", ce.getMessage());
                            info("cognee: cognify failed (soft)");
                        }
                    }

                    saveFile("cognee_add_" + datasetId,
                            "Cognee Add Result\n\nDataset: " + datasetId + "\n\n" + text,
                            "cognee add");

                    return JSON.toJSONString(out); // ✅ 返回 String
                }

                case "search": {
                    String query = asStr(in.get("query"));
                    if (query.isEmpty()) return err("query is required for search");
                    String searchType = asStrOr(in.get("search_type"), "GRAPH_COMPLETION");
                    int topK = (in.get("top_k") instanceof Number) ? ((Number) in.get("top_k")).intValue() : 5;

                    String datasetId = resolveDataset(in);
                    info("cognee: dataset=" + datasetId);

                    JSONObject body = new JSONObject();
                    body.put("searchType", searchType);
                    body.put("datasetIds", List.of(datasetId));
                    body.put("query", query);
                    body.put("topK", topK);

                    String resp = authedPost(BASE_URL + "/api/v1/search",
                            RequestBody.create(body.toJSONString(), MediaType.parse("application/json")));
                    Object parsed = parseJsonSafe(resp);

                    saveFile("cognee_search_" + datasetId,
                            "Cognee Search Result\n\nDataset: " + datasetId + "\nType: " + searchType +
                                    "\nTopK: " + topK + "\nQuery: " + query + "\n\n" + pretty(parsed),
                            "cognee search");

                    info("cognee: search done");

                    JSONObject out = new JSONObject();
                    out.put("op", "search");
                    out.put("status", "ok");
                    out.put("datasetId", datasetId);
                    out.put("query", query);
                    out.put("search_type", searchType);
                    out.put("top_k", topK);
                    out.put("data", parsed);
                    return JSON.toJSONString(out); // ✅ 返回 String
                }

                case "visualize": {
                    String datasetId = asStr(in.get("visualize_dataset_id"));
                    if (datasetId.isEmpty()) return err("visualize requires visualize_dataset_id");

                    HttpUrl url = Objects.requireNonNull(HttpUrl.parse(BASE_URL + "/api/v1/visualize"))
                            .newBuilder()
                            .addQueryParameter("dataset_id", datasetId)
                            .build();

                    Request.Builder rb = new Request.Builder().url(url).get();
                    attachAuth(rb);
                    try (Response r = http.newCall(rb.build()).execute()) {
                        String body = r.body() != null ? r.body().string() : "";
                        Object parsed = parseJsonSafe(body);
                        info("cognee: visualize status=" + r.code() + " dataset=" + datasetId);

                        saveFile("cognee_visualize_" + datasetId,
                                "Cognee Visualize\n\nDataset: " + datasetId + "\nStatus: " + r.code() + "\n\n" + pretty(parsed),
                                "cognee visualize");

                        JSONObject out = new JSONObject();
                        out.put("op", "visualize");
                        out.put("status", "ok");
                        out.put("datasetId", datasetId);
                        out.put("http_status", r.code());
                        out.put("data", parsed);
                        return JSON.toJSONString(out); // ✅ 返回 String
                    }
                }

                default:
                    return err("unsupported mode: " + mode);
            }
        } catch (Exception e) {
            log.error("{} cognee_memory exception", rid(), e);
            return err("exception: " + e.getMessage());
        } finally {
            log.info("{} cognee_memory cost={}ms", rid(), System.currentTimeMillis() - t0);
            info("cognee: done");
        }
    }

    // -------------------- Auth --------------------

    private void ensureAuth() throws Exception {
        if (cookieHeaders != null || bearerToken != null) return;
        if (!tryLogin()) {
            tryRegister();
            if (!tryLogin()) throw new RuntimeException("Auth failed: login after register still failed");
        }
    }

    private boolean tryLogin() {
        try {
            RequestBody form = new FormBody.Builder()
                    .add("username", EMAIL)
                    .add("password", PASSWORD)
                    .add("grant_type", "password")
                    .build();
            Request req = new Request.Builder()
                    .url(BASE_URL + "/api/v1/auth/login")
                    .post(form)
                    .build();
            try (Response r = http.newCall(req).execute()) {
                String body = r.body() != null ? r.body().string() : "";
                List<String> setCookies = r.headers().values("Set-Cookie");
                if (!setCookies.isEmpty()) {
                    this.cookieHeaders = Headers.of("Cookie", joinCookies(setCookies));
                    return true;
                }
                if (r.isSuccessful()) {
                    try {
                        JSONObject j = JSONObject.parseObject(body);
                        String token = j.getString("access_token");
                        if (token != null && !token.isEmpty()) {
                            this.bearerToken = token;
                            return true;
                        }
                    } catch (Exception ignore) {}
                }
                log.warn("{} login failed code={} body={}", rid(), r.code(), body);
                return false;
            }
        } catch (Exception e) {
            log.warn("{} login exception: {}", rid(), e.toString());
            return false;
        }
    }

    private void tryRegister() throws Exception {
        JSONObject j = new JSONObject();
        j.put("email", EMAIL);
        j.put("password", PASSWORD);
        j.put("is_verified", true);

        Request req = new Request.Builder()
                .url(BASE_URL + "/api/v1/auth/register")
                .post(RequestBody.create(j.toJSONString(), MediaType.parse("application/json")))
                .build();
        try (Response r = http.newCall(req).execute()) {
            String body = r.body() != null ? r.body().string() : "";
            log.info("{} register result code={} body={}", rid(), r.code(), body);
        }
    }

    private void attachAuth(Request.Builder rb) {
        if (cookieHeaders != null) {
            rb.headers(cookieHeaders);
        } else if (bearerToken != null) {
            rb.header("Authorization", "Bearer " + bearerToken);
        }
    }

    private String authedPost(String url, RequestBody body) throws Exception {
        Request.Builder rb = new Request.Builder().url(url).post(body);
        attachAuth(rb);
        try (Response r = http.newCall(rb.build()).execute()) {
            String resp = r.body() != null ? r.body().string() : "";
            if (!r.isSuccessful()) {
                throw new RuntimeException("HTTP " + r.code() + " " + url + " => " + resp);
            }
            return resp;
        }
    }

    private String authedPostWithReadTimeout(String url, RequestBody body, int readTimeoutSec) throws Exception {
        OkHttpClient temp = http.newBuilder()
                .readTimeout(readTimeoutSec, TimeUnit.SECONDS)
                .callTimeout(Math.max(5, readTimeoutSec + 5), TimeUnit.SECONDS)
                .build();
        Request.Builder rb = new Request.Builder().url(url).post(body);
        attachAuth(rb);
        try (Response r = temp.newCall(rb.build()).execute()) {
            String resp = r.body() != null ? r.body().string() : "";
            if (!r.isSuccessful()) {
                throw new RuntimeException("HTTP " + r.code() + " " + url + " => " + resp);
            }
            return resp;
        }
    }

    // -------- Dataset / Add / Cognify / Search / Visualize --------

    private String resolveDataset(Map<String, Object> in) throws Exception {
        String datasetId = asStr(in.get("dataset_id"));
        if (!datasetId.isEmpty()) return datasetId;

        String datasetName = asStr(in.get("dataset_name"));
        if (datasetName.isEmpty()) {
            String sid = (agentContext != null ? agentContext.getSessionId() : null);
            datasetName = (sid == null || sid.isEmpty())
                    ? "jdgenie_profile"
                    : ("jdgenie_profile_" + makeSafe(sid));
        }
        return ensureDataset(datasetName);
    }

    private String ensureDataset(String name) throws Exception {
        JSONObject req = new JSONObject();
        req.put("name", name);
        String resp = authedPost(BASE_URL + "/api/v1/datasets",
                RequestBody.create(req.toJSONString(), MediaType.parse("application/json")));
        JSONObject jr = JSONObject.parseObject(resp);
        String id = jr.getString("id");
        if (id == null || id.isEmpty() || "null".equals(id)) {
            throw new RuntimeException("create dataset but no id: " + resp);
        }
        return id;
    }

    private String addText(String datasetId, String text) throws Exception {
        File tmp = File.createTempFile("cognee_mem_", ".txt");
        try (FileWriter fw = new FileWriter(tmp, StandardCharsets.UTF_8)) {
            fw.write(text);
        }
        MediaType OCTET = MediaType.parse("application/octet-stream");
        RequestBody fileBody = RequestBody.create(tmp, OCTET);

        MultipartBody.Builder mb = new MultipartBody.Builder()
                .setType(MultipartBody.FORM)
                .addFormDataPart("datasetId", datasetId)
                .addFormDataPart("data", tmp.getName(), fileBody);

        Request.Builder rb = new Request.Builder()
                .url(BASE_URL + "/api/v1/add")
                .post(mb.build());
        attachAuth(rb);

        try (Response r = http.newCall(rb.build()).execute()) {
            String body = r.body() != null ? r.body().string() : "";
            if (!r.isSuccessful()) {
                throw new RuntimeException("HTTP " + r.code() + " /api/v1/add => " + body);
            }
            return body;
        } finally {
            // noinspection ResultOfMethodCallIgnored
            tmp.delete();
        }
    }

    private JSONObject cognify(List<String> datasetIds, boolean runInBackground, Integer waitSeconds) throws Exception {
        JSONObject req = new JSONObject();
        req.put("datasetIds", datasetIds);
        req.put("runInBackground", runInBackground);

        String resp;
        if (runInBackground || waitSeconds == null) {
            resp = authedPost(BASE_URL + "/api/v1/cognify",
                    RequestBody.create(req.toJSONString(), MediaType.parse("application/json")));
        } else {
            resp = authedPostWithReadTimeout(BASE_URL + "/api/v1/cognify",
                    RequestBody.create(req.toJSONString(), MediaType.parse("application/json")),
                    Math.max(15, waitSeconds));
        }
        return JSONObject.parseObject(resp);
    }

    // -------------------- File & Utils --------------------

    private void saveFile(String baseName, String content, String desc) {
        try {
            FileTool fileTool = new FileTool();
            fileTool.setAgentContext(agentContext);

            FileRequest fr = FileRequest.builder()
                    .requestId(agentContext != null ? agentContext.getRequestId() : "unknown")
                    .fileName(makeSafe(baseName) + ".md")
                    .description(desc)
                    .content(content)
                    .build();
            fileTool.uploadFile(fr, false, false);
            info("file saved: " + fr.getFileName());
        } catch (Throwable e) {
            log.warn("{} file upload skipped: {}", rid(), e.toString());
        }
    }

    private void info(String msg) {
        String line = "[cognee] " + msg;
        try {
            if (agentContext != null && Boolean.TRUE.equals(agentContext.getIsStream())
                    && agentContext.getPrinter() != null) {
                Method m = agentContext.getPrinter().getClass().getMethod("send", String.class);
                m.invoke(agentContext.getPrinter(), line);
            }
        } catch (Throwable ignore) { }
        log.info("{} {}", rid(), line);
    }

    private static Object parseJsonSafe(String s) {
        try { return JSON.parse(s); } catch (Exception ignore) { return s; }
    }

    private static String pretty(Object o) {
        try { return JSON.toJSONString(o, true); } catch (Exception e) { return String.valueOf(o); }
    }

    private String err(String msg) {
        // 统一返回 String，避免 BaseAgent 的 String 强转崩溃
        JSONObject j = new JSONObject();
        j.put("error", msg);
        return j.toJSONString();
    }

    private String rid() { return agentContext != null ? agentContext.getRequestId() : "-"; }

    private static String asStr(Object o) { return o == null ? "" : String.valueOf(o); }

    private static String asStrOr(Object o, String dft) { String s = asStr(o); return s.isEmpty() ? dft : s; }

    private static String makeSafe(String s) { return s.replaceAll("[\\\\/:*?\"<>|\\s]+", "_"); }

    private static String joinCookies(List<String> setCookies) {
        StringBuilder sb = new StringBuilder();
        for (String sc : setCookies) {
            String val = sc.split(";", 2)[0];
            if (sb.length() > 0) sb.append("; ");
            sb.append(val);
        }
        return sb.toString();
    }
}
