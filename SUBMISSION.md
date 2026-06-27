# Post-Call Processing Pipeline — Design Document

**Author:** Naveen Kumar
**Date:** 27/06/2026

---

## 1. Assumptions

_State every assumption you made about the business, system, or environment. Be specific. These will be discussed in the follow-up._

1. The platform processes post-call events for multiple customers simultaneously, and each customer may run multiple campaigns at the same time.

2. The LLM provider enforces both Requests Per Minute (RPM) and Tokens Per Minute (TPM) limits, and exceeding either limit results in rate limiting (HTTP 429).

3. The average token consumption per interaction is approximately 1,500 tokens, but actual usage may vary depending on transcript length.

4. Call transcripts are available immediately after a call ends, while call recordings may become available after a variable delay (typically 10–90 seconds).

5. Recording retrieval and transcript analysis are independent operations and can be executed concurrently.

6. Reliability is more important than low latency. No completed interaction should be permanently lost due to temporary infrastructure failures.

7. Customers should receive a fair share of LLM processing capacity, and one customer's high traffic should not impact other customers.

8. Existing API endpoints should remain backward compatible so that external systems do not require any changes.

---

## 2. Problem Diagnosis

_Before designing anything: what is actually broken, and why does it break at scale? In your own words._


The current post-call processing pipeline works correctly for low traffic but does not scale to large campaigns involving 100,000 or more calls. The major bottleneck is that LLM requests are sent immediately without checking Requests Per Minute (RPM) or Tokens Per Minute (TPM) limits, resulting in HTTP 429 rate-limit errors under heavy load.

The system also contains several architectural limitations that reduce reliability and scalability:

* A fixed `45-second` wait is used before fetching recordings, causing unnecessary delays and missed recordings if they become available later.
* Recording upload and LLM analysis are executed sequentially even though they are independent tasks, increasing total processing time.
* A single Celery queue processes all customers and all task types, allowing one customer's workload to delay others.
* Retry logic is duplicated between Celery and a Redis retry queue, which can lead to duplicate processing.
* Redis is used for both the primary queue and retry queue, creating a single point of failure.
* The circuit breaker reacts after overload instead of preventing it by controlling request flow.
* Logging and monitoring are insufficient to trace failed interactions or measure customer-level LLM usage.

Overall, the existing design is functional but lacks proper rate-limit management, fair resource allocation, durable task execution, and operational visibility required for production-scale workloads.

---

## 3. Architecture Overview

The redesigned architecture separates independent tasks, enforces LLM rate limits before processing, and improves reliability by introducing scheduling, customer budgeting, and durable processing.

                        Call Ends
                            │
                            ▼
                  FastAPI Webhook Endpoint
                            │
                            ▼
              Persist Processing Job & Audit Log
                            │
                            ▼
                  Priority Processing Queue
                            │
               ┌────────────┴────────────┐
               ▼                         ▼
     Recording Poller             LLM Scheduler
   (Retry + Backoff)       (RPM + TPM + Budget Check)
               │                         │
               ▼                         ▼
         Upload to S3              LLM Analysis
               └────────────┬────────────┘
                            ▼
              CRM Update & Lead Update
                            │
                            ▼
                 Audit Logs & Metrics

### Key design decisions

1.LLM requests pass through a scheduler before reaching the provider to ensure RPM and TPM limits are never exceeded.
2.Recording retrieval and LLM analysis run independently, reducing overall processing latency.
3.Customer-level token budgets ensure one customer's traffic does not consume the entire LLM capacity.
4.Priority-based processing allows important interactions to be processed before low-priority calls.
5.Structured audit logging records every processing stage, making debugging and monitoring easier.
6.Durable processing with retry support ensures failed interactions can be retried without permanent data loss.

---

## 4. Rate Limit Management

The redesigned system introduces a Rate Limit Scheduler between the processing queue and the LLM provider. Every LLM request must pass through the scheduler before it is executed. The scheduler checks both Requests Per Minute (RPM) and Tokens Per Minute (TPM) to ensure the provider's limits are never exceeded.


### How you track rate limit usage

The scheduler maintains two counters:

RPM Counter – Tracks the number of LLM requests sent during the current minute.
TPM Counter – Tracks the total tokens consumed during the current minute using the tokens_used value returned by the LLM response.

These counters are continuously updated and reset every minute.

### How you decide what to process now vs. defer

Before sending an LLM request, the scheduler:

1. Estimates the token requirement for the interaction.
2. Checks whether enough RPM and TPM capacity is available.
3. If sufficient capacity exists, the request is immediately processed.
4. If capacity is unavailable, the interaction remains in the processing queue until capacity becomes available.

This prevents unnecessary API failures and keeps processing within provider limits.

### What happens when the limit is hit (recovery, not crash)
Instead of sending requests that result in HTTP 429 errors, the scheduler temporarily delays low-priority interactions while continuing to process requests whenever capacity becomes available.

The system therefore applies backpressure rather than failing requests.

Benefits include:

-No unnecessary 429 errors
-Better utilization of available LLM capacity
-Stable processing during high traffic
-Predictable queue behaviour under load

## 5. Per-Customer Token Budgeting

_If total capacity is N tokens/min and K customers are active simultaneously:_

- How do you allocate capacity across customers?

Every customer is assigned a reserved Tokens Per Minute (TPM) budget based on their subscription or expected workload.
Reserved capacity is guaranteed and cannot be consumed by other customers.
Remaining unused capacity is placed into a shared pool that can be temporarily used by customers experiencing higher demand.
Total LLM Capacity = 90,000 TPM
example;
Customer A Reserved = 30,000 TPM
Customer B Reserved = 20,000 TPM
Customer C Reserved = 10,000 TPM

Shared Pool = 30,000 TPM

- What guarantees does a customer with a pre-allocated budget receive?

Each customer is guaranteed access to their reserved budget even during peak traffic. This ensures that one customer's large campaign cannot delay or block another customer's processing.

- What happens when a customer exceeds their budget?

If a customer consumes their reserved allocation:

-The scheduler first attempts to allocate tokens from the shared pool.
-If shared capacity is also exhausted, the interaction remains queued until tokens become available.
-The request is deferred, not rejected.

This prevents HTTP 429 errors while ensuring fair resource allocation.

- What happens to unallocated headroom?

Unused reserved capacity is temporarily contributed to the shared pool. This improves overall LLM utilization while still preserving fairness between customers.

---

## 6. Differentiated Processing

Not all completed calls require the same level of processing. The redesigned system classifies interactions based on business importance and processes high-priority interactions before low-priority ones.

A lightweight classification step is performed using the transcript and business metadata to determine the priority of each interaction.

High-priority interactions are processed immediately to minimize customer response time, while low-priority interactions are processed only when LLM capacity is available.

This approach reduces queue waiting time for business-critical interactions and improves overall customer experience.

Justification:

Using a lightweight classification step before full LLM analysis reduces unnecessary LLM usage and ensures that limited processing capacity is used for interactions that provide the highest business value.

---

## 7. Recording Pipeline

The fixed `asyncio.sleep(45)` is replaced with a polling mechanism using exponential backoff. Instead of waiting a fixed duration, the system checks for recording availability at increasing intervals (10s, 20s, 40s, 80s) until the recording is available or the maximum retry limit is reached.

Recording retrieval runs independently of LLM analysis because transcript processing does not depend on the audio recording. This reduces the overall processing latency.

If the recording is still unavailable after all retry attempts, the interaction is marked as **Recording Fetch Failed**, an error is logged, and an alert is generated so the issue can be investigated or replayed later.

---

## 8. Reliability & Durability

The redesigned system ensures that no completed interaction is permanently lost.

Each interaction is first stored as a durable processing job before execution. Failed tasks are retried using exponential backoff, and interactions that exceed the retry limit are moved to a Dead Letter Queue (DLQ) instead of being discarded.

Every interaction maintains a processing state such as **Queued**, **Processing**, **Completed**, or **Failed**, allowing recovery after worker failures or infrastructure restarts.

---

## 9. Auditability & Observability

Every stage of processing is recorded using structured audit logs so that any interaction can be traced even several days later.

### What you log (and what fields every log event includes)

Every log event contains:

- interaction_id
- customer_id
- campaign_id
- agent_id
- processing_stage
- timestamp
- retry_count
- tokens_used
- latency
- status

### Alert conditions

Alerts are generated for:

- Recording retrieval failures
- LLM processing failures
- Dead Letter Queue entries
- High queue depth
- High LLM utilization
- Customer token budget exhaustion

---

## 10. Data Model

_Schema changes required. Show the SQL._

```sql
CREATE TABLE processing_jobs (
    id UUID PRIMARY KEY,
    interaction_id UUID,
    customer_id UUID,
    status VARCHAR(30),
    priority VARCHAR(20),
    retry_count INT DEFAULT 0,
    created_at TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE TABLE customer_token_usage (
    customer_id UUID,
    minute_window TIMESTAMP,
    tokens_used INT,
    PRIMARY KEY(customer_id, minute_window)
);

CREATE TABLE audit_logs (
    id UUID PRIMARY KEY,
    interaction_id UUID,
    event_type VARCHAR(50),
    event_time TIMESTAMP,
    details JSONB
);
```

---

## 11. Security

The system processes customer conversations, recordings, and business information, making security a critical requirement.

Sensitive data is protected using HTTPS for communication, encrypted storage for recordings, environment variables for API keys and credentials, and role-based access control (RBAC) for accessing recordings and interaction data.

Audit logs avoid storing sensitive transcript content unless required for debugging.

---

## 12. API Interface

_Did you change the API contract (`POST /session/.../end`)? If yes, explain why. If no, explain why you kept it._

The existing API contract is **not changed**.

Keeping the API unchanged maintains backward compatibility with existing telephony providers and client applications. All improvements are implemented internally without requiring changes from external systems.

---

## 13. Trade-offs & Alternatives Considered

| Option | Why Considered | Why Rejected / What You Chose Instead |
|--------|---------------|--------------------------------------|
| Fixed 45-second wait | Simple implementation | Replaced with polling and exponential backoff to reduce latency and avoid missing recordings. |
| Direct LLM invocation | Lower implementation complexity | Replaced with a scheduler to enforce RPM and TPM limits before sending requests. |
| Single processing queue | Easy to maintain | Replaced with priority-based scheduling to improve fairness and reduce delays. |
| Redis-only retry | Already available | Added durable processing and Dead Letter Queue to avoid permanent task loss. |
| Binary circuit breaker | Prevent overload | Replaced with proactive scheduling and gradual backpressure instead of stopping all processing. |

---

## 14. Known Weaknesses

Although the redesigned system improves scalability and reliability, a few limitations remain:

- Customer budgets are statically allocated and could be made adaptive in future versions.
- Priority classification may require tuning based on business requirements.
- Dead Letter Queue replay still requires manual intervention.
- Extremely large traffic spikes may increase queue waiting time even though interactions are not lost.

---

## 15. What I Would Do With More Time

1. Implement dynamic customer token budgeting based on real-time traffic.
2. Build a monitoring dashboard showing queue depth, token usage, and worker health.
3. Add automatic Dead Letter Queue replay after transient failures.
4. Introduce distributed scheduling across multiple worker clusters.
5. Implement predictive auto-scaling based on campaign load.