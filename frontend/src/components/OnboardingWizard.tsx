import React, { useState, useEffect } from 'react';

interface OnboardingWizardProps {
  onComplete: (projectName: string, template?: string) => void;
  onSkip: () => void;
}

const STORAGE_KEY = 'fpulse_onboarding_done';

const templates = [
  {
    id: 'simple_etl',
    name: 'Simple ETL',
    description: 'Extract, transform, and load data between sources with a straightforward pipeline.',
    icon: (
      <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 7.5L7.5 3m0 0L12 7.5M7.5 3v13.5m13.5-6L16.5 15m0 0L12 10.5m4.5 4.5V1.5" />
      </svg>
    ),
  },
  {
    id: 'data_quality',
    name: 'Data Quality',
    description: 'Validate, cleanse, and profile your data with built-in quality checks.',
    icon: (
      <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75L11.25 15 15 9.75m-3-7.036A11.959 11.959 0 013.598 6 11.99 11.99 0 003 9.749c0 5.592 3.824 10.29 9 11.623 5.176-1.332 9-6.03 9-11.622 0-1.31-.21-2.571-.598-3.751h-.152c-3.196 0-6.1-1.248-8.25-3.285z" />
      </svg>
    ),
  },
  {
    id: 'aggregation',
    name: 'Aggregation Report',
    description: 'Filter, group, and summarize a dataset — produces a CSV report.',
    icon: (
      <svg className="w-6 h-6" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
        <path strokeLinecap="round" strokeLinejoin="round" d="M3 13.125C3 12.504 3.504 12 4.125 12h2.25c.621 0 1.125.504 1.125 1.125v6.75C7.5 20.496 6.996 21 6.375 21h-2.25A1.125 1.125 0 013 19.875v-6.75zM9.75 8.625c0-.621.504-1.125 1.125-1.125h2.25c.621 0 1.125.504 1.125 1.125v11.25c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V8.625zM16.5 4.125c0-.621.504-1.125 1.125-1.125h2.25C20.496 3 21 3.504 21 4.125v15.75c0 .621-.504 1.125-1.125 1.125h-2.25a1.125 1.125 0 01-1.125-1.125V4.125z" />
      </svg>
    ),
  },
];

function OnboardingWizard({ onComplete, onSkip }: OnboardingWizardProps) {
  const [step, setStep] = useState(0);
  const [projectName, setProjectName] = useState('My First Project');
  const [selectedTemplate, setSelectedTemplate] = useState<string | undefined>(undefined);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    const done = localStorage.getItem(STORAGE_KEY);
    if (done !== 'true') {
      setVisible(true);
    }
  }, []);

  const markDone = () => {
    localStorage.setItem(STORAGE_KEY, 'true');
  };

  const handleComplete = () => {
    markDone();
    onComplete(projectName.trim() || 'My First Project', selectedTemplate);
  };

  const handleSkip = () => {
    markDone();
    onSkip();
  };

  if (!visible) return null;

  const totalSteps = 4;

  const stepContent = [
    // Step 0: Welcome
    <div key="welcome" className="flex flex-col items-center text-center gap-4">
      <div className="w-16 h-16 rounded-2xl bg-amber-100 flex items-center justify-center">
        <svg className="w-9 h-9 text-amber-500" viewBox="0 0 44 48" fill="none" stroke="currentColor" strokeWidth={3} strokeLinecap="round" strokeLinejoin="round">
          <path d="M2 24 L17 24 L22 12 L28 36 L33 24 L42 24" />
        </svg>
      </div>
      <h2 className="text-2xl font-bold text-gray-900">Welcome to F-Pulse OSS</h2>
      <p className="text-gray-500 text-sm leading-relaxed max-w-sm">
        Build data pipelines visually with a drag-and-drop canvas. Connect nodes,
        configure transforms, and orchestrate your workflows — all from your browser.
      </p>
    </div>,

    // Step 1: Create Project
    <div key="project" className="flex flex-col items-center text-center gap-4">
      <div className="w-16 h-16 rounded-2xl bg-amber-100 flex items-center justify-center">
        <svg className="w-8 h-8 text-amber-500" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M2.25 12.75V12A2.25 2.25 0 014.5 9.75h15A2.25 2.25 0 0121.75 12v.75m-8.69-6.44l-2.12-2.12a1.5 1.5 0 00-1.061-.44H4.5A2.25 2.25 0 002.25 6v12a2.25 2.25 0 002.25 2.25h15A2.25 2.25 0 0021.75 18V9a2.25 2.25 0 00-2.25-2.25h-5.379a1.5 1.5 0 01-1.06-.44z" />
        </svg>
      </div>
      <h2 className="text-2xl font-bold text-gray-900">Create Your First Project</h2>
      <p className="text-gray-500 text-sm leading-relaxed max-w-sm">
        Projects keep your pipelines organized. Give yours a name to get started.
      </p>
      <input
        type="text"
        value={projectName}
        onChange={(e) => setProjectName(e.target.value)}
        placeholder="My First Project"
        className="w-full max-w-xs mt-2 px-4 py-2.5 rounded-lg border border-gray-300 text-sm text-gray-900 placeholder-gray-400 focus:outline-none focus:ring-2 focus:ring-amber-500 focus:border-amber-500 transition-colors"
        autoFocus
      />
    </div>,

    // Step 2: Pick Template
    <div key="template" className="flex flex-col items-center text-center gap-4">
      <h2 className="text-2xl font-bold text-gray-900">Try a Template</h2>
      <p className="text-gray-500 text-sm leading-relaxed max-w-sm">
        Start with a pre-built pipeline template, or skip this and build from scratch.
      </p>
      <div className="flex flex-col gap-3 w-full mt-2">
        {templates.map((tpl) => (
          <button
            key={tpl.id}
            onClick={() => setSelectedTemplate(selectedTemplate === tpl.id ? undefined : tpl.id)}
            className={`flex items-start gap-3 p-3.5 rounded-xl border-2 text-left transition-all ${
              selectedTemplate === tpl.id
                ? 'border-amber-500 bg-amber-50'
                : 'border-gray-200 hover:border-gray-300 bg-white'
            }`}
          >
            <div
              className={`flex-shrink-0 w-10 h-10 rounded-lg flex items-center justify-center ${
                selectedTemplate === tpl.id
                  ? 'bg-amber-500 text-white'
                  : 'bg-gray-100 text-gray-500'
              }`}
            >
              {tpl.icon}
            </div>
            <div className="min-w-0">
              <div className="font-medium text-sm text-gray-900">{tpl.name}</div>
              <div className="text-xs text-gray-500 mt-0.5">{tpl.description}</div>
            </div>
            {selectedTemplate === tpl.id && (
              <svg className="w-5 h-5 text-amber-500 flex-shrink-0 mt-0.5" fill="currentColor" viewBox="0 0 20 20">
                <path fillRule="evenodd" d="M10 18a8 8 0 100-16 8 8 0 000 16zm3.857-9.809a.75.75 0 00-1.214-.882l-3.483 4.79-1.88-1.88a.75.75 0 10-1.06 1.061l2.5 2.5a.75.75 0 001.137-.089l4-5.5z" clipRule="evenodd" />
              </svg>
            )}
          </button>
        ))}
      </div>
    </div>,

    // Step 3: Ready
    <div key="ready" className="flex flex-col items-center text-center gap-4">
      <div className="w-16 h-16 rounded-2xl bg-green-100 flex items-center justify-center">
        <svg className="w-8 h-8 text-green-600" fill="none" viewBox="0 0 24 24" stroke="currentColor" strokeWidth={2}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M15.59 14.37a6 6 0 01-5.84 7.38v-4.8m5.84-2.58a14.98 14.98 0 006.16-12.12A14.98 14.98 0 009.631 8.41m5.96 5.96a14.926 14.926 0 01-5.841 2.58m-.119-8.54a6 6 0 00-7.381 5.84h4.8m2.581-5.84a14.927 14.927 0 00-2.58 5.841m2.699 2.7c-.103.021-.207.041-.311.06a15.09 15.09 0 01-2.448-2.448 14.9 14.9 0 01.06-.312m-2.24 2.39a4.493 4.493 0 00-1.757 4.306 4.493 4.493 0 004.306-1.758M16.5 9a1.5 1.5 0 11-3 0 1.5 1.5 0 013 0z" />
        </svg>
      </div>
      <h2 className="text-2xl font-bold text-gray-900">You're Ready!</h2>
      <p className="text-gray-500 text-sm leading-relaxed max-w-sm">
        Here are a few tips to help you move fast:
      </p>
      <div className="w-full space-y-2.5 mt-1">
        {[
          { keys: 'Drag', label: 'Drag nodes from the sidebar onto the canvas' },
          { keys: 'Cmd+K', label: 'Open quick search to find anything' },
          { keys: 'Ctrl+S', label: 'Save your pipeline at any time' },
          // 2026-06-05 — surface the Steward in the first-run tour so
          // new users discover the headline differentiator. The eye
          // icon won't have findings yet on a fresh workspace, but
          // knowing what it is means they look at it later when their
          // workflow set has grown.
          { keys: 'Eye', label: 'The violet eye icon in the header is the Steward — F-Pulse\'s reliability watcher' },
        ].map((tip) => (
          <div key={tip.keys} className="flex items-center gap-3 px-4 py-2.5 rounded-lg bg-gray-50 text-left">
            <kbd className="flex-shrink-0 px-2 py-1 rounded-md bg-gray-200 text-xs font-mono font-medium text-gray-700">
              {tip.keys}
            </kbd>
            <span className="text-sm text-gray-600">{tip.label}</span>
          </div>
        ))}
      </div>
    </div>,
  ];

  const isLastStep = step === totalSteps - 1;

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-black/50 backdrop-blur-sm">
      <div className="relative w-full max-w-lg mx-4 bg-white rounded-2xl shadow-2xl overflow-hidden">
        {/* Skip button */}
        <button
          onClick={handleSkip}
          className="absolute top-4 right-4 text-gray-400 hover:text-gray-600 transition-colors text-sm"
        >
          Skip
        </button>

        {/* Content */}
        <div className="px-8 pt-10 pb-6">
          {stepContent[step]}
        </div>

        {/* Footer */}
        <div className="px-8 pb-8 flex items-center justify-between">
          {/* Back button */}
          <div>
            {step > 0 && (
              <button
                onClick={() => setStep(step - 1)}
                className="px-4 py-2 text-sm font-medium text-gray-500 hover:text-gray-700 transition-colors"
              >
                Back
              </button>
            )}
          </div>

          {/* Progress dots */}
          <div className="flex items-center gap-2">
            {Array.from({ length: totalSteps }).map((_, i) => (
              <div
                key={i}
                className={`transition-all duration-300 rounded-full ${
                  i === step
                    ? 'w-6 h-2 bg-amber-500'
                    : i < step
                    ? 'w-2 h-2 bg-amber-400'
                    : 'w-2 h-2 bg-gray-200'
                }`}
              />
            ))}
          </div>

          {/* Next / Finish button */}
          <div>
            {isLastStep ? (
              <button
                onClick={handleComplete}
                className="px-5 py-2.5 rounded-lg bg-amber-500 text-white text-sm font-semibold hover:bg-amber-600 transition-colors shadow-sm"
              >
                Start Building
              </button>
            ) : (
              <button
                onClick={() => setStep(step + 1)}
                className="px-5 py-2.5 rounded-lg bg-amber-500 text-white text-sm font-semibold hover:bg-amber-600 transition-colors shadow-sm"
              >
                Next
              </button>
            )}
          </div>
        </div>

        {/* Step indicators with checkmarks */}
        <div className="px-8 pb-6 flex items-center justify-center gap-4">
          {['Welcome', 'Project', 'Template', 'Ready'].map((label, i) => (
            <div key={label} className="flex items-center gap-1.5">
              <div
                className={`w-5 h-5 rounded-full flex items-center justify-center text-xs ${
                  i < step
                    ? 'bg-amber-500 text-white'
                    : i === step
                    ? 'border-2 border-amber-500 text-amber-500'
                    : 'border-2 border-gray-200 text-gray-300'
                }`}
              >
                {i < step ? (
                  <svg className="w-3 h-3" fill="currentColor" viewBox="0 0 20 20">
                    <path fillRule="evenodd" d="M16.707 5.293a1 1 0 010 1.414l-8 8a1 1 0 01-1.414 0l-4-4a1 1 0 011.414-1.414L8 12.586l7.293-7.293a1 1 0 011.414 0z" clipRule="evenodd" />
                  </svg>
                ) : (
                  <span className="font-medium">{i + 1}</span>
                )}
              </div>
              <span
                className={`text-xs font-medium ${
                  i <= step ? 'text-gray-700' : 'text-gray-400'
                }`}
              >
                {label}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}

export default OnboardingWizard;
