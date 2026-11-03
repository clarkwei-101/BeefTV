import { useEffect, useState } from "react";
import { useNavigate } from "react-router";
import { Button, Card, Form, Input, Typography, message } from "antd";
import { LockOutlined, UserOutlined } from "@ant-design/icons";

import { useSessionStore } from "@/stores/use-session-store";

const { Title, Paragraph } = Typography;

export default function AuthPage() {
    const navigate = useNavigate();
    const [mode, setMode] = useState<"login" | "register">("login");
    const needsSetup = useSessionStore((state) => state.needsSetup);
    const registrationOpen = useSessionStore((state) => state.registrationOpen);
    const busy = useSessionStore((state) => state.busy);
    const login = useSessionStore((state) => state.login);
    const register = useSessionStore((state) => state.register);
    const [form] = Form.useForm();

    useEffect(() => {
        if (needsSetup) setMode("register");
    }, [needsSetup]);

    // 公开注册关闭时不提供注册入口（首建管理员走 needsSetup 分支，不受影响）。
    useEffect(() => {
        if (!needsSetup && !registrationOpen) setMode("login");
    }, [needsSetup, registrationOpen]);

    async function handleSubmit(values: { username: string; password: string; confirm?: string }) {
        const username = values.username.trim();
        // 注册/登录失败必须把后端原因显示出来：store 的 login/register 会抛，
        // 这里不接住就会变成 unhandledrejection，页面毫无反馈，
        // 用户只会看到「点了没反应」（用户名占用、注册关闭、后端不可达都一样）。
        try {
            if (mode === "register") {
                if (values.password !== values.confirm) {
                    message.error("Two passwords do not match");
                    return;
                }
                await register(username, values.password);
                message.success(`Welcome, ${username}`);
            } else {
                await login(username, values.password);
                message.success(`Welcome back, ${username}`);
            }
        } catch (error) {
            message.error(error instanceof Error ? error.message : "操作失败，请稍后重试");
            return;
        }
        navigate("/", { replace: true });
    }

    return (
        <div
            style={{
                minHeight: "100dvh",
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                padding: 24,
                background: "var(--app-workspace-bg, #0f0f14)",
            }}
        >
            <Card style={{ width: 400, maxWidth: "100%" }} bordered={false}>
                <Title level={3} style={{ marginBottom: 4 }}>
                    {mode === "login" ? "Sign in to BeefTV" : needsSetup ? "Create admin account" : "Create BeefTV account"}
                </Title>
                <Paragraph type="secondary" style={{ marginBottom: 24 }}>
                    {mode === "login"
                        ? "Sign in with your username and password."
                        : needsSetup
                          ? "No account exists yet. The first account becomes the administrator."
                          : "Usernames are unique. The password must be at least 6 characters."}
                </Paragraph>
                <Form form={form} layout="vertical" onFinish={handleSubmit} autoComplete="off">
                    <Form.Item
                        name="username"
                        label="Username"
                        rules={[
                            { required: true, message: "Please enter your username" },
                            { min: 3, message: "At least 3 characters" },
                            { max: 32, message: "At most 32 characters" },
                        ]}
                    >
                        <Input prefix={<UserOutlined />} placeholder="e.g. director01" autoComplete="username" />
                    </Form.Item>
                    <Form.Item
                        name="password"
                        label="Password"
                        rules={[
                            { required: true, message: "Please enter your password" },
                            { min: 6, message: "At least 6 characters" },
                        ]}
                    >
                        <Input.Password prefix={<LockOutlined />} placeholder="At least 6 characters" autoComplete={mode === "login" ? "current-password" : "new-password"} />
                    </Form.Item>
                    {mode === "register" && (
                        <Form.Item
                            name="confirm"
                            label="Confirm password"
                            dependencies={["password"]}
                            rules={[
                                { required: true, message: "Please confirm your password" },
                                ({ getFieldValue }) => ({
                                    validator(_, value) {
                                        if (!value || getFieldValue("password") === value) return Promise.resolve();
                                        return Promise.reject(new Error("Two passwords do not match"));
                                    },
                                }),
                            ]}
                        >
                            <Input.Password prefix={<LockOutlined />} placeholder="Enter the password again" autoComplete="new-password" />
                        </Form.Item>
                    )}
                    <Form.Item style={{ marginBottom: 12 }}>
                        <Button type="primary" htmlType="submit" block loading={busy}>
                            {mode === "login" ? "Sign in" : "Create account"}
                        </Button>
                    </Form.Item>
                </Form>
                {!needsSetup && registrationOpen && (
                    <Button type="link" block onClick={() => { setMode(mode === "login" ? "register" : "login"); form.resetFields(["password", "confirm"]); }}>
                        {mode === "login" ? "No account? Create one" : "Have an account? Sign in"}
                    </Button>
                )}
                {!needsSetup && !registrationOpen && mode === "register" && (
                    <Paragraph type="secondary" style={{ textAlign: "center", marginBottom: 0 }}>
                        Public registration is closed. Please sign in with your account.
                    </Paragraph>
                )}
            </Card>
        </div>
    );
}
