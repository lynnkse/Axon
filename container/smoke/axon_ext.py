class OfflinePlugin:
    def system_prompt_context(self): return "Offline container smoke test."
    def on_turn_received(self, context): return None
    def context_for_turn(self, context): return ""
    def transform_response(self, context, response_text): return response_text
    def on_turn_completed(self, context, response_text): return None

def create_plugin():
    return OfflinePlugin()

