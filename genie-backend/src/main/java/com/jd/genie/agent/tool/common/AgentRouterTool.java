package com.jd.genie.agent.tool.common;

import com.alibaba.fastjson.JSON;
import com.alibaba.fastjson.JSONArray;
import com.alibaba.fastjson.JSONObject;
import com.jd.genie.agent.agent.AgentContext;
import com.jd.genie.agent.tool.BaseTool;
import lombok.Data;
import lombok.extern.slf4j.Slf4j;
import okhttp3.*;

import java.lang.reflect.Method;
import java.util.*;
import java.util.concurrent.TimeUnit;
import java.util.stream.Collectors;

/**
 *
 *
 * 环境变量/系统属性：
 *   AGENT_ROUTER_BASE_URL   默认 http://localhost:5001
 *   AGENT_ROUTER_CONNECT_TIMEOUT_MS 默认 3000
 *   AGENT_ROUTER_READ_TIMEOUT_MS    默认 15000
 */
@Slf4j
@Data
public class AgentRouterTool implements BaseTool {

    private AgentContext agentContext;

    // --- 配置：仅此一处，可被 env / -D 覆盖 ---
    private final String BASE_URL = getEnvOrProp("AGENT_ROUTER_BASE_URL", "http://localhost:5001");
    private final int CONNECT_TIMEOUT_MS = asInt(getEnvOrProp("AGENT_ROUTER_CONNECT_TIMEOUT_MS", "3000"), 3000);
    private final int READ_TIMEOUT_MS    = asInt(getEnvOrProp("AGENT_ROUTER_READ_TIMEOUT_MS", "600000"), 600000);

    private final OkHttpClient http = new OkHttpClient.Builder()
            .connectTimeout(CONNECT_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .readTimeout(READ_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .writeTimeout(READ_TIMEOUT_MS, TimeUnit.MILLISECONDS)
            .callTimeout(Math.max(READ_TIMEOUT_MS + 3000, 10000), TimeUnit.MILLISECONDS)
            .build();

    @Override
    public String getName() {
        return "agent_router";
    }

    @Override
    public String getDescription() {
        // 目标：把 /api/description 的总述 + /api/catalog 的每个智能体的完整描述，合并为一个长描述
        // 供函数调用系统作为 tool/function 的 description 使用，让 LLM 能“看见”各智能体的介绍
        String baseDesc =
                "将用户问题智能路由到最合适的下游智能体（支持 workflow 与普通远程 Agent），" +
                        "自动调用对应智能体 API 并返回结果。必填参数：query。可选：preferred_agent、force_reload。";

        StringBuilder sb = new StringBuilder(2048);

        // 1) 拉取 /api/description 基础描述
        try {
            JSONObject r = getJson(BASE_URL + "/api/description");
            if (r.getBooleanValue("success")) {
                String d = r.getString("description");
                if (d != null && !d.isEmpty()) {
                    baseDesc = d; // 以服务端返回为准
                }
            }
        } catch (Exception ignore) {}

        sb.append(baseDesc);

        // 2) 追加“可路由智能体目录”详述（来自 /api/catalog）
        try {
            JSONObject r = getJson(BASE_URL + "/api/catalog");
            if (r.getBooleanValue("success")) {
                JSONArray items = r.getJSONArray("items");
                if (items != null && !items.isEmpty()) {
                    sb.append("\n\n可路由智能体目录（").append(items.size()).append("）：");
                    for (int i = 0; i < items.size(); i++) {
                        JSONObject it = items.getJSONObject(i);
                        String name = nz(it.getString("name"));
                        String category = nz(it.getString("category"));
                        String version = nz(it.getString("version"));
                        String model = nz(it.getString("model"));
                        String apiUrl = "";
                        JSONObject api = it.getJSONObject("api");
                        if (api != null) {
                            // 常见两种：普通 agent 用 api.url；workflow 用 api.invoke_url
                            apiUrl = nz(api.getString("url"));
                            if (apiUrl.isEmpty()) apiUrl = nz(api.getString("invoke_url"));
                        }
                        String desc = nz(it.getString("description"));

                        // skills（只展示名称，避免过长；如需完整也可拼描述）
                        String skillsStr = "";
                        try {
                            JSONArray skills = it.getJSONArray("skills");
                            if (skills != null && !skills.isEmpty()) {
                                List<String> skillNames = new ArrayList<>();
                                for (int j = 0; j < skills.size(); j++) {
                                    JSONObject s = skills.getJSONObject(j);
                                    String sn = s.getString("name");
                                    if (sn != null && !sn.isEmpty()) skillNames.add(sn);
                                }
                                if (!skillNames.isEmpty()) {
                                    skillsStr = String.join("、", skillNames);
                                }
                            }
                        } catch (Exception ignore) {}

                        sb.append("\n- 名称：").append(name);
                        if (!category.isEmpty()) sb.append("（类型：").append(category).append("）");
                        if (!version.isEmpty()) sb.append("  版本：").append(version);
                        if (!model.isEmpty()) sb.append("  模型：").append(model);
                        if (!apiUrl.isEmpty()) sb.append("  API：").append(apiUrl);
                        if (!skillsStr.isEmpty()) sb.append("  技能：").append(skillsStr);
                        if (!desc.isEmpty()) {
                            sb.append("\n  简介：").append(desc.trim());
                        }
                    }

                    // 给 LLM 一个直觉提示 preferred_agent 的候选
                    List<String> names = items.stream()
                            .map(o -> (JSONObject) o)
                            .map(j -> j.getString("name"))
                            .filter(Objects::nonNull)
                            .collect(Collectors.toList());
                    if (!names.isEmpty()) {
                        sb.append("\n\n可选 preferred_agent：").append(String.join(" / ", names));
                    }
                }
            }
        } catch (Exception e) {
            log.warn("{} getDescription catalog fetch failed: {}", rid(), e.toString());
        }

        return sb.toString();
    }

    @Override
    public Map<String, Object> toParams() {
        // 从 /api/catalog 动态拿到可选 agent 列表，做成 preferred_agent 的 enum
        List<String> names = Collections.emptyList();
        try {
            JSONObject r = getJson(BASE_URL + "/api/catalog");
            if (r.getBooleanValue("success")) {
                JSONArray items = r.getJSONArray("items");
                if (items != null) {
                    names = items.stream()
                            .map(o -> (JSONObject) o)
                            .map(j -> j.getString("name"))
                            .filter(Objects::nonNull)
                            .collect(Collectors.toList());
                }
            }
        } catch (Exception e) {
            log.warn("{} agent_router toParams catalog fetch failed: {}", rid(), e.toString());
        }

        Map<String, Object> props = new LinkedHashMap<>();
        props.put("query", Map.of(
                "type", "string",
                "description", "用户问题/需求（必填）"
        ));

        Map<String, Object> pref = new LinkedHashMap<>();
        pref.put("type", "string");
        // 这里把候选也写进 description，进一步提示 LLM
        if (!names.isEmpty()) {
            pref.put("description", "可选偏好路由目标（仅提示路由器，不强制）。候选：" + String.join(" / ", names));
            pref.put("enum", names);
        } else {
            pref.put("description", "可选偏好路由目标（仅提示路由器，不强制）");
        }
        props.put("preferred_agent", pref);

        props.put("force_reload", Map.of(
                "type", "boolean",
                "description", "是否强制刷新目录缓存（通常无需设置）"
        ));

        Map<String, Object> schema = new LinkedHashMap<>();
        schema.put("type", "object");
        schema.put("properties", props);
        schema.put("required", List.of("query"));
        return schema;
    }

    // 小工具
    private static String nz(String s) { return (s == null) ? "" : s; }

    @SuppressWarnings("unchecked")
    @Override
    public Object execute(Object input) {
        long t0 = System.currentTimeMillis();
        try {
            Map<String, Object> in;
            if (input instanceof Map) in = (Map<String, Object>) input;
            else in = (Map<String, Object>) JSON.parse(JSON.toJSONString(input));

            String query = asStr(in.get("query")).trim();
            if (query.isEmpty()) return err("参数错误：query 不能为空");

            String preferred = asStr(in.get("preferred_agent")).trim();
            Boolean forceReload = (in.get("force_reload") instanceof Boolean) ? (Boolean) in.get("force_reload") : null;

            // 目前 /api/query 不支持显式 preferred_agent，这里用轻提示前置注入
            if (!preferred.isEmpty()) {
                query = "【preferred_agent=" + preferred + "】\n" + query;
            }

            JSONObject req = new JSONObject(true);
            req.put("query", query);
            if (forceReload != null) req.put("force_reload", forceReload);

            info("agent_router: POST /api/query ...");
            JSONObject resp = postJson(BASE_URL + "/api/query", req);
            if (resp == null) return err("AgentRouter 无响应");

            if (!resp.getBooleanValue("success")) {
                return err(resp.getString("error"));
            }

            // 统一取文本：优先 friendly_response -> response -> raw_response
            String text = firstNonEmpty(
                    resp.getString("friendly_response"),
                    resp.getString("response"),
                    resp.getString("raw_response")
            );

            if (text == null || text.isEmpty()) {
                String agent = resp.getString("routed_agent");
                Double conf = resp.getDouble("confidence");
                return "已路由至：" + safe(agent) + "（置信度 " + (conf == null ? "0.00" : String.format(Locale.ROOT,"%.2f", conf)) + "），但无文本输出。";
            }

            // 可选：将关键结果落盘成文件（如你们需要的话，取消注释）
            // saveFile("agent_router_result", text, "agent router result");

            return text;

        } catch (Exception e) {
            log.error("{} agent_router exception", rid(), e);
            return err("调用 AgentRouter 失败：" + e.getMessage());
        } finally {
            info("agent_router: done, cost=" + (System.currentTimeMillis() - t0) + "ms");
        }
    }

    // -------------------- HTTP helpers --------------------

    private JSONObject getJson(String url) throws Exception {
        Request req = new Request.Builder().url(url).get().build();
        try (Response r = http.newCall(req).execute()) {
            String body = r.body() != null ? r.body().string() : "";
            if (!r.isSuccessful()) {
                throw new RuntimeException("HTTP " + r.code() + " " + url + " => " + body);
            }
            return JSON.parseObject(body);
        }
    }

    private JSONObject postJson(String url, JSONObject json) throws Exception {
        RequestBody rb = RequestBody.create(json.toJSONString(), MediaType.parse("application/json"));
        Request req = new Request.Builder().url(url).post(rb).build();
        try (Response r = http.newCall(req).execute()) {
            String body = r.body() != null ? r.body().string() : "";
            if (!r.isSuccessful()) {
                throw new RuntimeException("HTTP " + r.code() + " " + url + " => " + body);
            }
            try { return JSON.parseObject(body); } catch (Exception ignore) { return new JSONObject(true).fluentPut("raw", body); }
        }
    }

    // -------------------- Utils --------------------

    private void info(String msg) {
        String line = "[agent_router] " + msg;
        try {
            if (agentContext != null && Boolean.TRUE.equals(agentContext.getIsStream())
                    && agentContext.getPrinter() != null) {
                Method m = agentContext.getPrinter().getClass().getMethod("send", String.class);
                m.invoke(agentContext.getPrinter(), line);
            }
        } catch (Throwable ignore) { }
        log.info("{} {}", rid(), line);
    }

    private String err(String msg) {
        JSONObject j = new JSONObject(true);
        j.put("error", msg);
        return j.toJSONString();
    }

    private String rid() { return agentContext != null ? agentContext.getRequestId() : "-"; }

    private static String asStr(Object o) { return o == null ? "" : String.valueOf(o); }

    private static String firstNonEmpty(String... ss) {
        for (String s : ss) if (s != null && !s.isEmpty()) return s;
        return null;
    }

    private static String safe(Object s) { return s == null ? "" : String.valueOf(s); }

    private static String getEnvOrProp(String key, String dft) {
        String v = System.getenv(key);
        if (v == null || v.isEmpty()) v = System.getProperty(key, dft);
        return v == null || v.isEmpty() ? dft : v;
    }

    private static int asInt(String s, int dft) {
        try { return Integer.parseInt(s); } catch (Exception ignore) { return dft; }
    }

}
