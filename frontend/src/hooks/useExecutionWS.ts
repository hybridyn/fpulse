import { useState, useEffect, useRef, useCallback } from 'react';

export interface ExecutionEvent {
  type: 'workflow_started' | 'step_started' | 'step_completed' | 'step_error' | 'workflow_completed';
  timestamp: string;
  workflow_id: string;
  step_id?: string;
  step_type?: string;
  row_count?: number;
  duration_ms?: number;
  error?: string;
  status?: string;
  progress?: number;
}

interface UseExecutionWSReturn {
  events: ExecutionEvent[];
  connected: boolean;
  execute: () => void;
  executeStep: (stepId: string) => void;
  cancel: () => void;
  lastEvent: ExecutionEvent | null;
  /** Map of step_id -> latest event for that step */
  stepStates: Record<string, ExecutionEvent>;
  /** Whether the workflow is currently running (between workflow_started and workflow_completed) */
  isExecuting: boolean;
  clearEvents: () => void;
}

/**
 * WebSocket hook for real-time workflow execution tracking.
 * Connects to ws://localhost:8001/ws/execution/{workflowId} and streams events.
 * Auto-reconnects on disconnect with exponential backoff.
 */
export function useExecutionWS(workflowId: string | null): UseExecutionWSReturn {
  const [events, setEvents] = useState<ExecutionEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [isExecuting, setIsExecuting] = useState(false);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const reconnectAttempt = useRef(0);
  const maxReconnectAttempts = 10;

  // Derived state
  const lastEvent = events.length > 0 ? events[events.length - 1] : null;
  const stepStates: Record<string, ExecutionEvent> = {};
  for (const evt of events) {
    if (evt.step_id) {
      stepStates[evt.step_id] = evt;
    }
  }

  const clearEvents = useCallback(() => {
    setEvents([]);
    setIsExecuting(false);
  }, []);

  const connect = useCallback(() => {
    if (!workflowId) return;

    // Clean up existing connection
    if (wsRef.current) {
      wsRef.current.close();
      wsRef.current = null;
    }

    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
    const host = window.location.hostname;
    const port = 8001;

    // Browsers can't set custom headers on `new WebSocket()`, so the
    // backend expects auth + workspace as query params. We read the
    // same localStorage keys the REST client uses, so the WS runs in
    // exactly the same tenant as the rest of the app.
    const token = localStorage.getItem('fpulse_token') || '';
    const workspaceId = localStorage.getItem('fpulse_workspace_id') || 'default';
    const qs = new URLSearchParams({
      token,
      workspace_id: workspaceId,
    }).toString();
    const url = `${protocol}//${host}:${port}/ws/execution/${workflowId}?${qs}`;

    try {
      const ws = new WebSocket(url);
      wsRef.current = ws;

      ws.onopen = () => {
        setConnected(true);
        reconnectAttempt.current = 0;
      };

      ws.onmessage = (msg) => {
        try {
          const event: ExecutionEvent = JSON.parse(msg.data);
          setEvents(prev => [...prev, event]);

          if (event.type === 'workflow_started') {
            setIsExecuting(true);
          }
          if (event.type === 'workflow_completed' || (event.type === 'step_error' && !event.step_id)) {
            setIsExecuting(false);
          }
        } catch {
          // Ignore malformed messages
        }
      };

      ws.onclose = () => {
        setConnected(false);
        wsRef.current = null;

        // Auto-reconnect with exponential backoff
        if (reconnectAttempt.current < maxReconnectAttempts) {
          const delay = Math.min(1000 * Math.pow(2, reconnectAttempt.current), 30000);
          reconnectAttempt.current += 1;
          reconnectTimer.current = setTimeout(connect, delay);
        }
      };

      ws.onerror = () => {
        // onclose will fire after onerror, handling reconnect
      };
    } catch {
      // WebSocket construction failed (e.g., invalid URL)
      setConnected(false);
    }
  }, [workflowId]);

  // Connect when workflowId changes
  useEffect(() => {
    connect();

    return () => {
      if (reconnectTimer.current) {
        clearTimeout(reconnectTimer.current);
        reconnectTimer.current = null;
      }
      if (wsRef.current) {
        wsRef.current.close();
        wsRef.current = null;
      }
    };
  }, [connect]);

  const sendCommand = useCallback((command: string, payload?: Record<string, any>) => {
    if (wsRef.current && wsRef.current.readyState === WebSocket.OPEN) {
      wsRef.current.send(JSON.stringify({ command, ...payload }));
    }
  }, []);

  const execute = useCallback(() => {
    setEvents([]);
    setIsExecuting(true);
    sendCommand('execute');
  }, [sendCommand]);

  const executeStep = useCallback((stepId: string) => {
    sendCommand('execute_step', { step_id: stepId });
  }, [sendCommand]);

  const cancel = useCallback(() => {
    sendCommand('cancel');
    setIsExecuting(false);
  }, [sendCommand]);

  return {
    events,
    connected,
    execute,
    executeStep,
    cancel,
    lastEvent,
    stepStates,
    isExecuting,
    clearEvents,
  };
}
