from src.config import settings


class RateLimitScheduler:
    """
    Controls LLM request execution by enforcing
    Requests Per Minute (RPM) and Tokens Per Minute (TPM) limits.
    """

    def __init__(self):
        self.max_requests = settings.LLM_REQUESTS_PER_MINUTE
        self.max_tokens = settings.LLM_TOKENS_PER_MINUTE

        self.current_requests = 0
        self.current_tokens = 0

    def estimate_tokens(self) -> int:
        """Returns estimated token usage per interaction."""
        return settings.LLM_AVG_TOKENS_PER_CALL

    def can_process_request(self) -> bool:
        """
        Check if another LLM request can be processed
        without exceeding RPM or TPM limits.
        """
        estimated_tokens = self.estimate_tokens()

        if self.current_requests + 1 > self.max_requests:
            return False

        if self.current_tokens + estimated_tokens > self.max_tokens:
            return False

        return True

    def reserve_capacity(self):
        """
        Reserve one request and the estimated number of tokens.
        """
        self.current_requests += 1
        self.current_tokens += self.estimate_tokens()

    def release_capacity(self, actual_tokens: int = None):
        """
        Release reserved capacity after processing completes.
        """
        self.current_requests = max(0, self.current_requests - 1)

        tokens = (
            actual_tokens
            if actual_tokens is not None
            else self.estimate_tokens()
        )

        self.current_tokens = max(0, self.current_tokens - tokens)


scheduler = RateLimitScheduler()