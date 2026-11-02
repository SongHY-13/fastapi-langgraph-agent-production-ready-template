flowchart TD
    UserMsg([用户消息]) --> EmotionalSupport
    subgraph EmotionEngine [情感处理引擎]
        CrisisIntervention[_crisis_intervention 节点]
        EmotionRiskCheck{emotion_support 风险判断<br/>risk=HIGH/CRISIS<br/>或自伤/他伤/紧急?}
        CancelPendingTools[取消/忽略未完成工具<br/>记录审计]
        ToolJoin[工具结果汇合<br/>等待并行工具完成或超时<br/>失败降级]
        ToolPlanner["_tool_planner 工具规划节点<br/>在 NormalChat 之前决定调用哪些工具"]
        EmotionalSupport["emotional_support Tool<br/>输出emotion_label, need_emotional_support, risk, intent等情绪标签"]
        RAGTool["RAG Tool 知识库检索增强"]
        WebSearchTool["Web搜索 Tool 实时信息获取"]
        ToolResultFilter["_tool_result_filter<br/>统一工具结果安全过滤<br/>情绪支持/RAG/Web 都走这里"]
        NormalChat["正常 chat 节点<br/>只基于已准备好的上下文生成回复<br/>不再自行调用工具"]

        EmotionalSupport --> EmotionRiskCheck

        EmotionRiskCheck -->|是：HIGH/CRISIS| CancelPendingTools
        CancelPendingTools --> CrisisIntervention
        EmotionRiskCheck -->|否：非高危，携带情绪结果| IntentRecognition
        IntentRecognition --> IntentClearCheck

        IntentClearCheck -->|不清晰| AskHuman
        AskHuman --> WaitUserReply
        WaitUserReply --> ClarifyRoundCheck
        ClarifyRoundCheck -->|是：继续多轮澄清| EmotionalSupport
        ClarifyRoundCheck -->|否：达到上限/取消| ClarifyFallback

        IntentClearCheck -->|清晰| ToolPlanner
        IntentClearCheck -->|清晰：携带情绪+意图+澄清记录| ToolJoin

        ToolPlanner -->|按需，可并行 fan-out| RAGTool
        ToolPlanner -->|按需，可并行 fan-out| WebSearchTool
        ToolPlanner -->|无需外部工具| ToolJoin
        RAGTool --> ToolJoin
        WebSearchTool --> ToolJoin

        ToolJoin --> ToolResultFilter
        ToolResultFilter --> ContextBuilder
        ContextBuilder --> NormalChat
    end

    CrisisIntervention --> OutputGuard
    NormalChat --> OutputGuard

    subgraph OutputGuardEngine [最终输出安全过滤]
        OutputGuard[_output_guard 最终输出安全过滤<br/>自伤/他伤/医疗法律边界/隐私/仇恨/越狱/危机模板校验]
        SafeRewrite[安全改写/带约束重生成<br/>注入违规原因与安全约束<br/>移除敏感工具结果<br/>降低温度/限制长度]
        SafeTemplate[预定安全模板<br/>通用/医疗法律/隐私/越狱/工具失败]
        CrisisTemplate[危机安全模板+地区化紧急资源]
        RetryCheck{重试次数 < N ?}

        OutputGuard -->|PASS| Pass[通过]
        OutputGuard -->|SOFT_RISK| SafeRewrite
        OutputGuard -->|HARD_BLOCK| SafeTemplate
        OutputGuard -->|CRISIS_HIGH| CrisisTemplate

        SafeRewrite --> RetryCheck
        RetryCheck -->|是| OutputGuard
        RetryCheck -->|否| SafeTemplate

        Pass --> FinalOut[最终回复]
        SafeTemplate --> FinalOut
        CrisisTemplate --> Handoff[可选：转人工/危机干预]
        Handoff --> FinalOut
    end

    FinalOut --> SaveSession[保存会话/审计日志<br/>风险等级/情绪/工具调用/安全事件]
    SaveSession --> EndNode((结束))
