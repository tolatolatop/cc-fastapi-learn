from cc_fastapi.workflows.provider_prompt import ProviderPromptTaskWorkflow


class GitHubPromptTaskWorkflow(ProviderPromptTaskWorkflow):
    def __init__(self) -> None:
        super().__init__("github", supersede_actions={"synchronize"})
