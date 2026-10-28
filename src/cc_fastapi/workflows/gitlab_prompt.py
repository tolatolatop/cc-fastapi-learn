from cc_fastapi.workflows.provider_prompt import ProviderPromptTaskWorkflow


class GitLabPromptTaskWorkflow(ProviderPromptTaskWorkflow):
    def __init__(self) -> None:
        super().__init__("gitlab", supersede_actions={"update"})
