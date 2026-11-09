"use client";

import React, { useState, useEffect } from "react";
import Link from "next/link";
import {
  Database,
  Cpu,
  Brain,
  GitBranch,
  MessageSquare,
  CheckCircle2,
  AlertCircle,
  RefreshCw,
  Copy,
  Check,
  Terminal,
  ArrowRight,
  Zap,
  Save,
  Trash2,
  Eye,
  EyeOff,
  Sparkles,
} from "lucide-react";

interface FormValues {
  databaseUrl: string;
  switchUrl: string;
  switchWsUrl: string;
  llmEndpoint: string;
  llmApiKey: string;
  llmModel: string;
  githubAppId: string;
  githubInstallationId: string;
  githubPemPath: string;
  targetRepo: string;
  discordBotToken: string;
  discordGuildId: string;
  discordChannelId: string;
  biscuitRoleId: string;
  opencodeRoleId: string;
}

const EMPTY_FORM: FormValues = {
  databaseUrl: "",
  switchUrl: "",
  switchWsUrl: "",
  llmEndpoint: "",
  llmApiKey: "",
  llmModel: "simple-task",
  githubAppId: "",
  githubInstallationId: "",
  githubPemPath: "",
  targetRepo: "",
  discordBotToken: "",
  discordGuildId: "",
  discordChannelId: "",
  biscuitRoleId: "",
  opencodeRoleId: "",
};

const DEMO_PRESETS: FormValues = {
  databaseUrl: "postgresql://switch_app:a202deadabf3d139eb242465ce3cfa68c51066618c621b32@localhost:5433/switch",
  switchUrl: "http://127.0.0.1:8000",
  switchWsUrl: "ws://127.0.0.1:8000",
  llmEndpoint: "http://localhost:5580/v1",
  llmApiKey: "sk-pkpi5wen5ifxugoer2t1sl2k8l2gprsj3eyk71lxl98vp6pz",
  llmModel: "simple-task",
  githubAppId: "5631771",
  githubInstallationId: "179018445",
  githubPemPath: "/Users/adarshchauhan/Downloads/biscuitt-hemlooo.2026-11-05.private-key.pem",
  targetRepo: "MRIARC-08/test",
  discordBotToken: "",
  discordGuildId: "1568866378147041342",
  discordChannelId: "1568866379095081124",
  biscuitRoleId: "1569289793622511677",
  opencodeRoleId: "1569289760260890644",
};

interface TestResult {
  loading: boolean;
  success?: boolean;
  latencyMs?: number;
  details?: any;
  error?: string;
}

export default function SetupPage() {
  const [form, setForm] = useState<FormValues>(EMPTY_FORM);
  const [loadingStatus, setLoadingStatus] = useState(true);
  const [saveStatus, setSaveStatus] = useState<string | null>(null);
  const [showApiKey, setShowApiKey] = useState(false);
  const [copied, setCopied] = useState(false);
  const [showEnvModal, setShowEnvModal] = useState(false);

  const [tests, setTests] = useState<Record<string, TestResult>>({
    db: { loading: false },
    switch: { loading: false },
    llm: { loading: false },
    github: { loading: false },
    discord: { loading: false },
  });

  const loadStatus = async () => {
    try {
      const res = await fetch("/api/setup/status");
      const json = await res.json();
      if (json.formValues) {
        setForm(json.formValues);
      }
    } catch (e) {
      console.error("Failed to load setup status", e);
    } finally {
      setLoadingStatus(false);
    }
  };

  useEffect(() => {
    loadStatus();
  }, []);

  const handleChange = (key: keyof FormValues, val: string) => {
    setForm((prev) => ({ ...prev, [key]: val }));
    // Reset test state for that component when edited
    setSaveStatus(null);
  };

  const handleSave = async () => {
    setSaveStatus("Saving...");
    try {
      const res = await fetch("/api/setup/save-env", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const json = await res.json();
      if (json.success) {
        setSaveStatus("✓ Saved to .env");
        setTimeout(() => setSaveStatus(null), 3000);
      } else {
        setSaveStatus("❌ " + json.error);
      }
    } catch (e: any) {
      setSaveStatus("❌ " + e.message);
    }
  };

  const handleReset = async () => {
    if (!confirm("Are you sure you want to clear all credentials? This will remove the .env file.")) return;
    try {
      await fetch("/api/setup/reset-env", { method: "POST" });
      setForm(EMPTY_FORM);
      setTests({
        db: { loading: false },
        switch: { loading: false },
        llm: { loading: false },
        github: { loading: false },
        discord: { loading: false },
      });
      setSaveStatus("✓ Credentials Cleared");
      setTimeout(() => setSaveStatus(null), 2500);
    } catch (e: any) {
      alert("Failed to reset: " + e.message);
    }
  };

  const handleLoadPresets = () => {
    setForm(DEMO_PRESETS);
    setSaveStatus("Presets loaded (Click Save to persist)");
  };

  const runTest = async (key: string, endpoint: string) => {
    setTests((prev) => ({ ...prev, [key]: { loading: true, error: undefined, success: undefined } }));
    try {
      const res = await fetch(endpoint, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(form),
      });
      const json = await res.json();
      setTests((prev) => ({
        ...prev,
        [key]: {
          loading: false,
          success: json.success,
          latencyMs: json.latencyMs,
          details: json.details,
          error: json.error,
        },
      }));
    } catch (e: any) {
      setTests((prev) => ({
        ...prev,
        [key]: { loading: false, success: false, error: e.message },
      }));
    }
  };

  const allPassed =
    tests.db.success &&
    tests.switch.success &&
    tests.llm.success &&
    tests.github.success &&
    tests.discord.success;

  return (
    <div style={{ minHeight: "100vh", backgroundColor: "#0b0f19", color: "#f1f5f9", fontFamily: "system-ui, -apple-system, sans-serif" }}>
      {/* Header */}
      <header style={{ borderBottom: "1px solid #1e293b", backgroundColor: "#080c14", padding: "14px 28px", position: "sticky", top: 0, zIndex: 50 }}>
        <div style={{ maxWidth: 1180, margin: "0 auto", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
          <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
            <div style={{ width: 34, height: 34, borderRadius: 8, background: "linear-gradient(135deg, #f59e0b, #d97706)", display: "flex", alignItems: "center", justifyContent: "center", fontWeight: "bold", fontSize: 16 }}>
              🍪
            </div>
            <div>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <span style={{ fontWeight: 700, fontSize: 17, letterSpacing: "-0.02em" }}>Biscuit & Switch Hub</span>
                <span style={{ fontSize: 11, background: "#1e293b", color: "#38bdf8", padding: "2px 8px", borderRadius: 12, border: "1px solid #334155" }}>
                  Zero-Config Mode
                </span>
              </div>
              <p style={{ margin: 0, fontSize: 11, color: "#64748b" }}>Self-Service Credential & Connectivity Manager</p>
            </div>
          </div>

          <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
            <button
              onClick={handleReset}
              style={{ display: "flex", alignItems: "center", gap: 6, background: "#1e1b2e", color: "#f43f5e", border: "1px solid rgba(244, 63, 94, 0.3)", borderRadius: 6, padding: "7px 12px", fontSize: 12, cursor: "pointer", fontWeight: 600 }}
              title="Clear all credentials to test fresh onboarding"
            >
              <Trash2 size={13} />
              Reset All
            </button>
            <button
              onClick={handleLoadPresets}
              style={{ display: "flex", alignItems: "center", gap: 6, background: "#1e293b", color: "#f59e0b", border: "1px solid rgba(245, 158, 11, 0.3)", borderRadius: 6, padding: "7px 12px", fontSize: 12, cursor: "pointer", fontWeight: 600 }}
              title="Fill recommended local credentials"
            >
              <Sparkles size={13} />
              Fill Presets
            </button>
            <button
              onClick={handleSave}
              style={{ display: "flex", alignItems: "center", gap: 6, background: "#0284c7", color: "#fff", border: "none", borderRadius: 6, padding: "7px 14px", fontSize: 12, cursor: "pointer", fontWeight: 600 }}
            >
              <Save size={13} />
              Save to .env
            </button>
          </div>
        </div>
      </header>

      {/* Main Container */}
      <main style={{ maxWidth: 1180, margin: "0 auto", padding: "36px 20px" }}>
        {/* Banner if saved */}
        {saveStatus && (
          <div style={{ marginBottom: 20, padding: "10px 16px", borderRadius: 8, background: saveStatus.startsWith("✓") ? "rgba(16, 185, 129, 0.15)" : "rgba(244, 63, 94, 0.15)", border: `1px solid ${saveStatus.startsWith("✓") ? "#10b981" : "#f43f5e"}`, fontSize: 13, color: saveStatus.startsWith("✓") ? "#10b981" : "#f43f5e", fontWeight: 600, display: "flex", alignItems: "center", gap: 8 }}>
            {saveStatus}
          </div>
        )}

        {/* Hero Section */}
        <div style={{ marginBottom: 28, display: "flex", justifyContent: "space-between", alignItems: "flex-end", flexWrap: "wrap", gap: 16 }}>
          <div>
            <div style={{ display: "inline-flex", alignItems: "center", gap: 6, background: "rgba(56, 189, 248, 0.1)", color: "#38bdf8", padding: "4px 10px", borderRadius: 16, fontSize: 11, fontWeight: 700, marginBottom: 8 }}>
              <Zap size={13} /> INTERACTIVE CONNECTION MANAGER
            </div>
            <h1 style={{ fontSize: 26, fontWeight: 800, margin: "0 0 6px 0", letterSpacing: "-0.03em" }}>
              Configure & Verify Infrastructure
            </h1>
            <p style={{ fontSize: 14, color: "#94a3b8", margin: 0 }}>
              Enter your credentials below and test each component on the fly. No manual terminal scripts required.
            </p>
          </div>

          <button
            onClick={() => {
              runTest("db", "/api/setup/test-db");
              runTest("switch", "/api/setup/test-switch");
              runTest("llm", "/api/setup/test-llm");
              runTest("github", "/api/setup/test-github");
              runTest("discord", "/api/setup/test-discord");
            }}
            style={{ display: "flex", alignItems: "center", gap: 8, background: "#10b981", color: "#fff", border: "none", borderRadius: 8, padding: "10px 18px", fontSize: 13, fontWeight: 700, cursor: "pointer", boxShadow: "0 4px 14px rgba(16, 185, 129, 0.25)" }}
          >
            <RefreshCw size={15} />
            Test All Components
          </button>
        </div>

        {/* 5 Component Cards */}
        <div style={{ display: "flex", flexDirection: "column", gap: 18 }}>
          {/* 1. PostgreSQL Panel */}
          <PanelCard
            icon={<Database size={20} color="#38bdf8" />}
            title="1. PostgreSQL & Living Decision Memory"
            description="Schema 2 relational tables holding verified quorums and architectural rulings"
            status={form.databaseUrl ? (tests.db.success ? "success" : tests.db.error ? "error" : "configured") : "unconfigured"}
            test={tests.db}
            onTest={() => runTest("db", "/api/setup/test-db")}
            buttonLabel="Test Database"
          >
            <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
              <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>DATABASE_URL (Connection String)</label>
              <input
                type="text"
                value={form.databaseUrl}
                onChange={(e) => handleChange("databaseUrl", e.target.value)}
                placeholder="postgresql://user:password@localhost:5432/dbname"
                style={inputStyle}
              />
            </div>
          </PanelCard>

          {/* 2. Switch Core Panel */}
          <PanelCard
            icon={<Cpu size={20} color="#a855f7" />}
            title="2. Switch Core Orchestrator (Agent Bus)"
            description="WebSocket event router connecting Biscuit Architect and OpenCode Coder"
            status={form.switchUrl ? (tests.switch.success ? "success" : tests.switch.error ? "error" : "configured") : "unconfigured"}
            test={tests.switch}
            onTest={() => runTest("switch", "/api/setup/test-switch")}
            buttonLabel="Test Switch Bus"
          >
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>SWITCH_URL (HTTP Gateway)</label>
                <input
                  type="text"
                  value={form.switchUrl}
                  onChange={(e) => handleChange("switchUrl", e.target.value)}
                  placeholder="http://127.0.0.1:8000"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>SWITCH_WS_URL (WebSocket Stream)</label>
                <input
                  type="text"
                  value={form.switchWsUrl}
                  onChange={(e) => handleChange("switchWsUrl", e.target.value)}
                  placeholder="ws://127.0.0.1:8000"
                  style={inputStyle}
                />
              </div>
            </div>
          </PanelCard>

          {/* 3. AI Inference Panel */}
          <PanelCard
            icon={<Brain size={20} color="#f59e0b" />}
            title="3. AI Reasoning & Decision Intelligence"
            description="Inference provider used for consensus extraction and AST PR diff analysis"
            status={form.llmEndpoint && form.llmApiKey ? (tests.llm.success ? "success" : tests.llm.error ? "error" : "configured") : "unconfigured"}
            test={tests.llm}
            onTest={() => runTest("llm", "/api/setup/test-llm")}
            buttonLabel="Ping AI Model"
          >
            <div style={{ display: "grid", gridTemplateColumns: "1.2fr 1fr 0.8fr", gap: 12 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>LOCAL_MODEL_ENDPOINT</label>
                <input
                  type="text"
                  value={form.llmEndpoint}
                  onChange={(e) => handleChange("llmEndpoint", e.target.value)}
                  placeholder="http://localhost:5580/v1"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>LOCAL_MODEL_API_KEY</label>
                <div style={{ position: "relative" }}>
                  <input
                    type={showApiKey ? "text" : "password"}
                    value={form.llmApiKey}
                    onChange={(e) => handleChange("llmApiKey", e.target.value)}
                    placeholder="sk-..."
                    style={{ ...inputStyle, width: "100%", paddingRight: 36 }}
                  />
                  <button
                    type="button"
                    onClick={() => setShowApiKey(!showApiKey)}
                    style={{ position: "absolute", right: 10, top: 10, background: "none", border: "none", color: "#64748b", cursor: "pointer" }}
                  >
                    {showApiKey ? <EyeOff size={15} /> : <Eye size={15} />}
                  </button>
                </div>
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>MODEL_NAME</label>
                <input
                  type="text"
                  value={form.llmModel}
                  onChange={(e) => handleChange("llmModel", e.target.value)}
                  placeholder="simple-task / gpt-4o"
                  style={inputStyle}
                />
              </div>
            </div>
          </PanelCard>

          {/* 4. GitHub Watchdog Panel */}
          <PanelCard
            icon={<GitBranch size={20} color="#f43f5e" />}
            title="4. GitHub App & PR Watchdog"
            description="Authenticates as GitHub App to inspect AST diffs and comment on Pull Requests"
            status={form.githubAppId && form.githubInstallationId && form.githubPemPath && form.targetRepo ? (tests.github.success ? "success" : tests.github.error ? "error" : "configured") : "unconfigured"}
            test={tests.github}
            onTest={() => runTest("github", "/api/setup/test-github")}
            buttonLabel="Verify GitHub App"
          >
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 10 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>GITHUB_APP_ID</label>
                <input
                  type="text"
                  value={form.githubAppId}
                  onChange={(e) => handleChange("githubAppId", e.target.value)}
                  placeholder="5631771"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>GITHUB_INSTALLATION_ID</label>
                <input
                  type="text"
                  value={form.githubInstallationId}
                  onChange={(e) => handleChange("githubInstallationId", e.target.value)}
                  placeholder="179018445"
                  style={inputStyle}
                />
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1.4fr 1fr", gap: 12 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>GITHUB_APP_PRIVATE_KEY_PATH (PEM file path)</label>
                <input
                  type="text"
                  value={form.githubPemPath}
                  onChange={(e) => handleChange("githubPemPath", e.target.value)}
                  placeholder="/Users/.../private-key.pem"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>TARGET_REPO (owner/repo)</label>
                <input
                  type="text"
                  value={form.targetRepo}
                  onChange={(e) => handleChange("targetRepo", e.target.value)}
                  placeholder="MRIARC-08/test"
                  style={inputStyle}
                />
              </div>
            </div>
          </PanelCard>

          {/* 5. Discord Bridge Panel */}
          <PanelCard
            icon={<MessageSquare size={20} color="#6366f1" />}
            title="5. Discord Bridge & Silent Observer"
            description="Discord bot integration mapping chat channels to Switch rooms"
            status={form.discordGuildId && form.discordChannelId && form.biscuitRoleId ? (tests.discord.success ? "success" : tests.discord.error ? "error" : "configured") : "unconfigured"}
            test={tests.discord}
            onTest={() => runTest("discord", "/api/setup/test-discord")}
            buttonLabel="Test Discord Bridge"
          >
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginBottom: 10 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>DISCORD_GUILD_ID (Server ID)</label>
                <input
                  type="text"
                  value={form.discordGuildId}
                  onChange={(e) => handleChange("discordGuildId", e.target.value)}
                  placeholder="1568866378147041342"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>DISCORD_CHANNEL_ID (Target Channel)</label>
                <input
                  type="text"
                  value={form.discordChannelId}
                  onChange={(e) => handleChange("discordChannelId", e.target.value)}
                  placeholder="1568866379095081124"
                  style={inputStyle}
                />
              </div>
            </div>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>BISCUIT_ROLE_ID (@biscuit-architect)</label>
                <input
                  type="text"
                  value={form.biscuitRoleId}
                  onChange={(e) => handleChange("biscuitRoleId", e.target.value)}
                  placeholder="1569289793622511677"
                  style={inputStyle}
                />
              </div>
              <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
                <label style={{ fontSize: 12, color: "#94a3b8", fontWeight: 600 }}>OPENCODE_ROLE_ID (@opencode-coder)</label>
                <input
                  type="text"
                  value={form.opencodeRoleId}
                  onChange={(e) => handleChange("opencodeRoleId", e.target.value)}
                  placeholder="1569289760260890644"
                  style={inputStyle}
                />
              </div>
            </div>
          </PanelCard>
        </div>

        {/* Success Banner */}
        {allPassed && (
          <div style={{ marginTop: 28, padding: 22, borderRadius: 10, background: "linear-gradient(135deg, rgba(16, 185, 129, 0.15), rgba(5, 150, 105, 0.05))", border: "1px solid #10b981", display: "flex", justifyContent: "space-between", alignItems: "center" }}>
            <div>
              <h3 style={{ margin: "0 0 4px 0", fontSize: 17, fontWeight: 700, color: "#10b981" }}>
                🎉 All 5 Core Systems Connected & Verified!
              </h3>
              <p style={{ margin: 0, fontSize: 13, color: "#94a3b8" }}>
                Your custom credentials connect cleanly. The living decision store and autonomous coding daemon are ready.
              </p>
            </div>
            <Link
              href="/"
              style={{ background: "#10b981", color: "#fff", textDecoration: "none", padding: "9px 18px", borderRadius: 6, fontWeight: 700, fontSize: 13, display: "inline-flex", alignItems: "center", gap: 6 }}
            >
              Open Dashboard <ArrowRight size={15} />
            </Link>
          </div>
        )}
      </main>
    </div>
  );
}

function PanelCard({
  icon,
  title,
  description,
  status,
  test,
  onTest,
  buttonLabel,
  children,
}: {
  icon: React.ReactNode;
  title: string;
  description: string;
  status: "unconfigured" | "configured" | "success" | "error";
  test: TestResult;
  onTest: () => void;
  buttonLabel: string;
  children: React.ReactNode;
}) {
  const getBadge = () => {
    if (test.success) {
      return (
        <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "#10b981", background: "rgba(16, 185, 129, 0.1)", border: "1px solid rgba(16, 185, 129, 0.3)", padding: "3px 8px", borderRadius: 12, fontWeight: 600 }}>
          <CheckCircle2 size={13} /> Verified ({test.latencyMs}ms)
        </span>
      );
    }
    if (test.error) {
      return (
        <span style={{ display: "flex", alignItems: "center", gap: 4, fontSize: 11, color: "#f43f5e", background: "rgba(244, 63, 94, 0.1)", border: "1px solid rgba(244, 63, 94, 0.3)", padding: "3px 8px", borderRadius: 12, fontWeight: 600 }}>
          <AlertCircle size={13} /> Failed
        </span>
      );
    }
    if (status === "configured") {
      return (
        <span style={{ fontSize: 11, color: "#f59e0b", background: "rgba(245, 158, 11, 0.1)", border: "1px solid rgba(245, 158, 11, 0.3)", padding: "3px 8px", borderRadius: 12, fontWeight: 600 }}>
          Configured (Untested)
        </span>
      );
    }
    return (
      <span style={{ fontSize: 11, color: "#94a3b8", background: "#1e293b", border: "1px solid #334155", padding: "3px 8px", borderRadius: 12 }}>
        Not Configured
      </span>
    );
  };

  return (
    <div style={{ backgroundColor: "#0f172a", border: test.success ? "1px solid #10b981" : test.error ? "1px solid #f43f5e" : "1px solid #1e293b", borderRadius: 10, padding: 20 }}>
      <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-start", marginBottom: 14, flexWrap: "wrap", gap: 10 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div style={{ background: "#080c14", padding: 8, borderRadius: 8, border: "1px solid #1e293b", display: "flex", alignItems: "center", justifyContent: "center" }}>
            {icon}
          </div>
          <div>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <h3 style={{ margin: 0, fontSize: 15, fontWeight: 700 }}>{title}</h3>
              {getBadge()}
            </div>
            <p style={{ margin: "2px 0 0 0", fontSize: 12, color: "#64748b" }}>{description}</p>
          </div>
        </div>

        <button
          onClick={onTest}
          disabled={test.loading}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            background: test.loading ? "#334155" : "#1e293b",
            color: "#f1f5f9",
            border: "1px solid #334155",
            borderRadius: 6,
            padding: "6px 12px",
            fontSize: 12,
            fontWeight: 600,
            cursor: test.loading ? "not-allowed" : "pointer",
          }}
        >
          <RefreshCw size={12} className={test.loading ? "animate-spin" : ""} />
          {test.loading ? "Testing..." : buttonLabel}
        </button>
      </div>

      <div style={{ marginBottom: 10 }}>{children}</div>

      {/* Error readout */}
      {test.error && (
        <div style={{ marginTop: 10, fontSize: 12, color: "#f43f5e", background: "rgba(244, 63, 94, 0.08)", padding: "8px 12px", borderRadius: 6, border: "1px solid rgba(244, 63, 94, 0.2)" }}>
          <b>Diagnostic Failure:</b> {test.error}
        </div>
      )}

      {/* Success readout */}
      {test.details && test.success && (
        <div style={{ marginTop: 10, fontSize: 12, color: "#10b981", background: "rgba(16, 185, 129, 0.08)", padding: "8px 12px", borderRadius: 6, border: "1px solid rgba(16, 185, 129, 0.2)" }}>
          <b>Connection Verified ({test.latencyMs}ms):</b> {JSON.stringify(test.details).slice(0, 140)}...
        </div>
      )}
    </div>
  );
}

const inputStyle: React.CSSProperties = {
  backgroundColor: "#080c14",
  border: "1px solid #1e293b",
  borderRadius: 6,
  padding: "8px 12px",
  color: "#f8fafc",
  fontSize: 13,
  outline: "none",
  fontFamily: "monospace",
};
