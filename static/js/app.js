/**
 * ChannelView - Main Application JavaScript
 * SPA-style routing with server-rendered shell
 */

// ==================== UTILITIES ====================

async function api(methodOrUrl, urlOrOpts = {}, body) {
  // Support both calling conventions:
  //   api('/url', {method:'POST', body:...})  — new style
  //   api('GET', '/url')                      — legacy style
  //   api('POST', '/url', {key:'val'})        — legacy style with body
  let url, opts;
  const HTTP_METHODS = ['GET','POST','PUT','DELETE','PATCH','HEAD','OPTIONS'];
  if (HTTP_METHODS.includes(methodOrUrl)) {
    url = urlOrOpts;
    opts = { method: methodOrUrl };
    if (body) opts.body = JSON.stringify(body);
  } else {
    url = methodOrUrl;
    opts = typeof urlOrOpts === 'object' ? urlOrOpts : {};
  }
  const csrf = (document.cookie.match(/csrf_token=([^;]+)/) || [])[1] || '';
  const res = await fetch(url, {
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf, ...opts.headers },
    ...opts
  });
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { throw new Error('Server returned an invalid response. Please refresh and try again.'); }
  if (!res.ok) throw new Error(data.error || 'Request failed');
  return data;
}
const apiFetch = api;

// ==================== PLAN GATING & UPGRADE PROMPTS ====================

// Cache billing/usage data so we only fetch once per session
let _planCache = null;
async function getPlanData() {
  if (_planCache) return _planCache;
  try {
    _planCache = await api('GET', '/api/billing/usage');
  } catch(e) {
    // Fallback — assume free plan if billing endpoint fails
    _planCache = { plan: 'free', features: {}, is_trial: false };
  }
  return _planCache;
}
// Invalidate cache when plan changes (e.g. after upgrade)
function clearPlanCache() { _planCache = null; }

// Map pages to the feature flag they require (only gated pages listed here)
const PAGE_FEATURE_GATE = {
  'ai':              { feature: 'ai_scoring',         label: 'AI Scoring',            icon: '🧠', desc: 'Let AI score your candidates automatically so you can focus on the best ones first.' },
  'ai-insights':     { feature: 'ai_scoring',         label: 'AI Scoring',            icon: '🧠', desc: 'Let AI score your candidates automatically so you can focus on the best ones first.' },
  'ai-scoring':      { feature: 'ai_scoring',         label: 'AI Scoring',            icon: '🤖', desc: 'Candidates are scored and ranked automatically based on their interview answers.' },
  'analytics':       { feature: 'advanced_analytics', label: 'Numbers & Trends',      icon: '📊', desc: 'See how your recruiting is going — how many candidates, how fast you\'re hiring, what\'s working.' },
  'analytics-dashboard': { feature: 'advanced_analytics', label: 'Numbers & Trends', icon: '📊', desc: 'Your recruiting dashboard with real-time numbers and trends at a glance.' },
  'admin-analytics': { feature: 'advanced_analytics', label: 'Admin Numbers',        icon: '📈', desc: 'See recruiting numbers across your whole team — all interviews, candidates, and activity.' },
  'reports':         { feature: 'advanced_analytics', label: 'Reports',              icon: '📋', desc: 'Pull reports on your hiring activity, export data, and track how you\'re doing over time.' },
  'report-hub':      { feature: 'advanced_analytics', label: 'All Reports',          icon: '📋', desc: 'One place for all your recruiting reports, exports, and scheduled report delivery.' },
  'pipeline-funnel': { feature: 'advanced_analytics', label: 'Hiring Funnel',        icon: '🔄', desc: 'See where candidates drop off in your hiring process — from first contact to hired.' },
  'white-label':     { feature: 'white_label',        label: 'Custom Look & Feel',    icon: '🎨', desc: 'Put your agency\'s logo, colors, and branding on everything candidates see.' },
  'branding':        { feature: 'white_label',        label: 'Branding',              icon: '🎨', desc: 'Make ChannelView look like yours — add your logo, colors, and agency name.' },
  'bulk-ops':        { feature: 'bulk_ops',            label: 'Bulk Actions',          icon: '⚡', desc: 'Send invitations to a bunch of candidates at once, update statuses in bulk, and save time.' },
  'integrations':    { feature: 'integrations',        label: 'Integrations',          icon: '🔌', desc: 'Connect ChannelView with the other tools you use — your AMS, CRM, email, and more.' },
  'integrations-hub':{ feature: 'integrations',        label: 'Integrations',          icon: '🔌', desc: 'Browse and set up connections with the other tools your agency uses.' },
  'ams-integrations':{ feature: 'integrations',        label: 'AMS Connection',        icon: '🔗', desc: 'Sync your candidates with your Agency Management System so nothing falls through the cracks.' },
  'api_docs':        { feature: 'api_access',          label: 'API Access',            icon: '🛠️', desc: 'For tech teams — full API access to build custom workflows and connect to your systems.' },
  'api-management':  { feature: 'api_access',          label: 'API Keys',              icon: '🔑', desc: 'Manage your API keys and see how they\'re being used.' },
  'voice-agent':     { feature: 'integrations',        label: 'Phone Screening',       icon: '🎙️', desc: 'An AI phone call that pre-screens candidates before you spend time on a video interview.' },
  'automation':      { feature: 'integrations',        label: 'Automation',            icon: '⚙️', desc: 'Set it and forget it — auto-send invites, follow-ups, and move candidates along your pipeline.' },
  'auto-rules':      { feature: 'integrations',        label: 'Auto-Rules',            icon: '⚙️', desc: 'Create simple rules like "if a candidate finishes, send me a notification" to save time.' },
};

/**
 * Check if current plan has access to a page. If not, render an upgrade overlay.
 * Returns true if page is BLOCKED (caller should stop rendering), false if allowed.
 */
async function checkPageGate(pageKey) {
  const gate = PAGE_FEATURE_GATE[pageKey];
  if (!gate) return false; // Not gated — allow

  const data = await getPlanData();
  const features = data.features || {};

  // If feature is enabled, allow
  if (features[gate.feature]) return false;

  // Feature is locked — render upgrade overlay
  const el = document.getElementById('page-content');
  const planName = data.plan || 'free';
  const minPlan = gate.feature === 'api_access' ? 'Professional' : 'Professional';

  el.innerHTML = `
    <div style="max-width:560px;margin:60px auto;text-align:center">
      <div style="font-size:56px;margin-bottom:16px;opacity:0.9">${gate.icon}</div>
      <h1 style="font-size:26px;margin:0 0 8px;color:#111">${gate.label}</h1>
      <p style="color:#666;font-size:15px;margin:0 0 24px;line-height:1.6">${gate.desc}</p>

      <div style="background:#f9fafb;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <div style="font-size:13px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:8px">Your Current Plan</div>
        <div style="font-size:20px;font-weight:700;color:#111;margin-bottom:4px">${planName.charAt(0).toUpperCase() + planName.slice(1)}</div>
        <div style="font-size:13px;color:#999">${gate.label} requires the <strong style="color:#0ace0a">${minPlan}</strong> plan or higher</div>
      </div>

      <button class="btn btn-primary" onclick="APP_PAGE='billing';loadPage()" style="font-size:15px;padding:14px 32px;min-width:220px">
        View Plans & Upgrade
      </button>
      <p style="margin-top:12px;font-size:12px;color:#999">Upgrade anytime — no long-term commitment required</p>
    </div>
  `;

  return true; // Blocked
}

function toast(msg, type = '') {
  const c = document.getElementById('toast-container');
  const t = document.createElement('div');
  t.className = 'toast ' + type;
  t.textContent = msg;
  c.appendChild(t);
  setTimeout(() => { t.style.opacity = '0'; t.style.transform = 'translateX(40px)'; setTimeout(() => t.remove(), 300); }, 3000);
}
const showToast = toast;

function formatDate(d) {
  if (!d) return '—';
  return new Date(d).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
}

function statusBadge(status) {
  const map = {
    invited: 'badge-blue', in_progress: 'badge-yellow', completed: 'badge-primary',
    reviewed: 'badge-green', hired: 'badge-green', rejected: 'badge-red',
    active: 'badge-green', paused: 'badge-yellow', closed: 'badge-gray'
  };
  return `<span class="badge ${map[status] || 'badge-gray'}">${status.replace('_', ' ')}</span>`;
}

function scoreColor(score) {
  if (score >= 80) return 'var(--success)';
  if (score >= 60) return 'var(--warning)';
  return 'var(--danger)';
}

function scoreRing(score, size = 60) {
  if (!score) return '<span style="color:#999">—</span>';
  const r = (size - 8) / 2;
  const c = 2 * Math.PI * r;
  const p = c * (1 - score / 100);
  const color = scoreColor(score);
  return `<div class="score-ring" style="width:${size}px;height:${size}px">
    <svg width="${size}" height="${size}"><circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="#e5e7eb" stroke-width="4"/>
    <circle cx="${size/2}" cy="${size/2}" r="${r}" fill="none" stroke="${color}" stroke-width="4" stroke-dasharray="${c}" stroke-dashoffset="${p}" stroke-linecap="round"/></svg>
    <span class="value" style="font-size:${size/4}px;color:${color}">${Math.round(score)}</span>
  </div>`;
}

function renderCategoryBreakdown(scoresJson) {
  if (!scoresJson) return '';
  let data;
  try { data = typeof scoresJson === 'string' ? JSON.parse(scoresJson) : scoresJson; } catch(e) { return ''; }
  const cats = data.categories;
  const labels = data.labels || {
    communication: 'Communication',
    industry_knowledge: 'Industry Knowledge',
    role_competence: 'Role Competence',
    culture_fit: 'Culture Fit',
    problem_solving: 'Problem Solving'
  };
  if (!cats) return '';
  const sorted = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  return `
    <div style="margin-top:20px;text-align:left;border-top:1px solid #e5e7eb;padding-top:16px">
      <h4 style="font-size:13px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px">Category Breakdown</h4>
      ${sorted.map(([key, score]) => {
        const color = score >= 80 ? '#059669' : (score >= 60 ? '#d97706' : '#dc2626');
        return `
          <div style="margin-bottom:10px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
              <span style="font-size:13px;font-weight:500;color:#333">${labels[key] || key}</span>
              <span style="font-size:13px;font-weight:700;color:${color}">${Math.round(score)}</span>
            </div>
            <div style="width:100%;height:8px;background:#e5e7eb;border-radius:4px;overflow:hidden">
              <div style="width:${score}%;height:100%;background:${color};border-radius:4px;transition:width .6s ease"></div>
            </div>
          </div>`;
      }).join('')}
    </div>`;
}

function renderResponseCategories(scoresJson) {
  if (!scoresJson) return '';
  let cats;
  try { cats = typeof scoresJson === 'string' ? JSON.parse(scoresJson) : scoresJson; } catch(e) { return ''; }
  const labels = {
    communication: 'Comm',
    industry_knowledge: 'Industry',
    role_competence: 'Role',
    culture_fit: 'Culture',
    problem_solving: 'Problem'
  };
  const sorted = Object.entries(cats).sort((a,b) => b[1] - a[1]);
  return `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:8px">
    ${sorted.map(([key, score]) => {
      const color = score >= 80 ? '#059669' : (score >= 60 ? '#d97706' : '#dc2626');
      return `<span style="display:inline-flex;align-items:center;gap:4px;padding:3px 8px;background:${color}10;border-radius:6px;font-size:11px;font-weight:600;color:${color}">${labels[key] || key} ${Math.round(score)}</span>`;
    }).join('')}
  </div>`;
}

async function logout() {
  await fetch('/api/auth/logout', { method: 'POST' });
  window.location.href = '/login';
}

// ==================== PAGE ROUTER ====================

const content = document.getElementById('page-content');

const pages = {
  dashboard: renderDashboard,
  interviews: renderInterviews,
  interview_builder: renderInterviewBuilder,
  interview_detail: renderInterviewDetail,
  candidates: renderCandidates,
  review: renderReview,
  ai: renderAiInsights,
  analytics: renderAnalytics,
  settings: renderSettings,
  billing: renderBilling,
  onboarding: renderOnboarding,
  admin: renderAdmin,
  branding: renderBranding,
  automation: renderAutomation,
  api_docs: renderApiDocs,
  integrations: renderIntegrations,
  compliance: renderCompliance,
  kanban: renderKanban,
  system: renderSystem,
  reports: renderReports,
  security: renderSecurity,
  team: renderTeam,
  activity: renderActivity,
  deploy: renderDeploy,
  'admin-analytics': renderAdminAnalytics,
  'ai-scoring': renderAiScoring,
  'integrations-hub': renderIntegrationsHub,
  'review-hub': renderReviewHub,
  'fmo': renderFmoPortal,
  'email-templates': renderEmailTemplates,
  'white-label': renderWhiteLabel,
  'report-hub': renderReportHub,
  'bulk-ops': renderBulkOps,
  'audit': renderAuditTrail,
  'video-library': renderVideoLibrary,
  'ai-insights': renderAiInsightsC20,
  'notification-settings': renderNotificationSettings,
  'ams-integrations': renderAmsIntegrations,
  'api-management': renderApiManagement,
  'demo-manager': renderDemoManager,
  'analytics-dashboard': renderAnalyticsDashboard,
  'email-delivery': renderEmailDelivery,
  'onboarding-wizard': renderOnboardingWizard,
  'help-center': renderHelpCenter,
  'global-search': renderGlobalSearch,
  'profile-settings': renderProfileSettings,
  'data-management': renderDataManagement,
  'security-settings': renderSecuritySettings,
  'activity-log': renderActivityLog,
  'pipeline-funnel': renderPipelineFunnel,
  'enhanced-kanban': renderEnhancedKanban,
  'auto-rules': renderAutoRules,
  'custom-stages': renderCustomStages,
  'source-tracking': renderSourceTracking,
  'job-board': renderJobBoardSettings,
  'candidate-experience': renderCandidateExperience,
  'lead-sourcing': renderLeadSourcing,
  'referral-links': renderReferralLinks,
  'job-syndication': renderJobSyndication,
  'voice-agent': renderVoiceAgent,
};

async function loadPage() {
  // Check for first-login onboarding before loading normal page
  try {
    const onb = await api('GET', '/api/onboarding/setup-status');
    if (onb.needs_setup) { renderOnboardingWizard(); return; }
  } catch(e) { /* proceed normally if onboarding check fails */ }

  if (pages[APP_PAGE]) {
    // Check plan gating before rendering page
    const blocked = await checkPageGate(APP_PAGE);
    if (blocked) return; // Upgrade prompt shown — don't render page

    try { await pages[APP_PAGE](); }
    catch (err) { content.innerHTML = `<div class="empty-state"><h3>Error loading page</h3><p>${err.message}</p></div>`; }
  }
}

// ==================== CYCLE 30: ONBOARDING WIZARD ====================

async function renderOnboardingWizard() {
  const el = document.getElementById('page-content');
  el.innerHTML = `
    <div style="max-width:560px;margin:40px auto">
      <div style="text-align:center;margin-bottom:32px">
        <div style="font-size:32px;margin-bottom:8px">👋</div>
        <h1 style="font-size:28px;margin:0 0 8px">Welcome to ChannelView</h1>
        <p style="color:#666;font-size:15px;margin:0">Let's get your agency set up in just a few steps.</p>
      </div>

      <!-- Progress Steps -->
      <div style="display:flex;justify-content:center;gap:8px;margin-bottom:32px" id="onb-steps">
        <div class="onb-step active" data-step="1"><span>1</span> Security</div>
        <div class="onb-step" data-step="2"><span>2</span> Profile</div>
        <div class="onb-step" data-step="3"><span>3</span> Get Started</div>
      </div>

      <style>
        .onb-step { padding:8px 16px;border-radius:20px;font-size:13px;font-weight:500;color:#999;background:#f3f4f6;transition:all 0.2s }
        .onb-step.active { background:#0ace0a;color:#000;font-weight:700 }
        .onb-step.done { background:#e6fce6;color:#0ace0a }
        .onb-step span { font-weight:700 }
      </style>

      <!-- Step 1: Change Password -->
      <div id="onb-panel" class="card">
        <h2 style="font-size:20px;margin:0 0 8px">Set Your Password</h2>
        <p style="color:#666;font-size:14px;margin:0 0 20px">Your account was created with a temporary password. Choose a secure password you'll remember.</p>
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">New Password</label>
        <input type="password" id="onb-password" class="form-input" placeholder="At least 8 characters" style="margin-bottom:12px">
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Confirm Password</label>
        <input type="password" id="onb-password2" class="form-input" placeholder="Type it again" style="margin-bottom:20px">
        <button class="btn btn-primary" style="width:100%" onclick="onbChangePassword()">Set Password & Continue</button>
      </div>
    </div>
  `;
}

async function onbChangePassword() {
  const pw = document.getElementById('onb-password').value;
  const pw2 = document.getElementById('onb-password2').value;
  if (pw.length < 8) { toast('Password must be at least 8 characters', 'error'); return; }
  if (pw !== pw2) { toast('Passwords do not match', 'error'); return; }

  try {
    const res = await api('POST', '/api/onboarding/change-password', { new_password: pw });
    if (res.success) {
      toast('Password updated!', 'success');
      // Move to step 2
      document.querySelectorAll('.onb-step').forEach(s => s.classList.remove('active'));
      document.querySelector('.onb-step[data-step="1"]').classList.add('done');
      document.querySelector('.onb-step[data-step="2"]').classList.add('active');

      document.getElementById('onb-panel').innerHTML = `
        <h2 style="font-size:20px;margin:0 0 8px">Your Agency Profile</h2>
        <p style="color:#666;font-size:14px;margin:0 0 20px">Confirm your details so your team knows who you are.</p>
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Your Name</label>
        <input id="onb-name" class="form-input" value="${APP_USER.name||''}" style="margin-bottom:12px">
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Agency Name</label>
        <input id="onb-agency" class="form-input" value="${APP_USER.agency_name||''}" style="margin-bottom:12px">
        <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Phone (optional)</label>
        <input id="onb-phone" class="form-input" placeholder="(555) 123-4567" style="margin-bottom:20px">
        <button class="btn btn-primary" style="width:100%" onclick="onbUpdateProfile()">Save & Continue</button>
      `;
    } else toast(res.error || 'Failed to change password', 'error');
  } catch(e) { toast(e.message, 'error'); }
}

async function onbUpdateProfile() {
  const name = document.getElementById('onb-name').value.trim();
  const agency = document.getElementById('onb-agency').value.trim();
  const phone = document.getElementById('onb-phone').value.trim();
  if (!name || !agency) { toast('Name and agency name are required', 'error'); return; }

  try {
    const res = await api('POST', '/api/onboarding/update-profile', { name, agency_name: agency, phone });
    if (res.success) {
      toast('Profile saved!', 'success');
      // Move to step 3
      document.querySelectorAll('.onb-step').forEach(s => s.classList.remove('active'));
      document.querySelector('.onb-step[data-step="2"]').classList.add('done');
      document.querySelector('.onb-step[data-step="3"]').classList.add('active');

      document.getElementById('onb-panel').innerHTML = `
        <div style="text-align:center">
          <div style="font-size:48px;margin-bottom:12px">🎉</div>
          <h2 style="font-size:22px;margin:0 0 8px">You're All Set!</h2>
          <p style="color:#666;font-size:14px;margin:0 0 8px">Your <strong>30-day Professional trial</strong> is active.</p>
          <p style="color:#888;font-size:13px;margin:0 0 24px">200 candidates/mo, 25 interviews, AI scoring, and more — all free for 30 days.</p>
          <div style="display:flex;flex-direction:column;gap:12px;max-width:300px;margin:0 auto">
            <button class="btn btn-primary" style="width:100%;font-size:15px;padding:14px" onclick="onbComplete('interviews')">Create Your First Interview</button>
            <button class="btn btn-outline" style="width:100%" onclick="onbComplete('dashboard')">Go to Dashboard</button>
          </div>
        </div>
      `;
    } else toast(res.error || 'Failed', 'error');
  } catch(e) { toast(e.message, 'error'); }
}

async function onbComplete(destination) {
  try { await api('POST', '/api/onboarding/complete'); } catch(e) { /* ok */ }
  window.location.href = '/' + (destination || 'dashboard');
}

// ==================== DASHBOARD ====================

async function renderDashboard() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading dashboard...</div>';
  try {
    const data = await api('/api/dashboard');
    const s = data.stats;
    content.innerHTML = `
      <div class="page-header">
        <div><h1>Dashboard</h1><p class="subtitle">Welcome back, ${APP_USER.name}</p></div>
        <div class="page-actions">
          <button class="btn btn-primary" onclick="window.location.href='/interviews/new'">+ New Interview</button>
        </div>
      </div>
      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Active Interviews</div><div class="stat-value">${s.active_interviews}</div></div>
        <div class="stat-card"><div class="stat-label">Total Candidates</div><div class="stat-value">${s.total_candidates}</div></div>
        <div class="stat-card"><div class="stat-label">Completed</div><div class="stat-value">${s.completed}</div></div>
        <div class="stat-card"><div class="stat-label">Avg AI Score</div><div class="stat-value">${s.avg_score || '—'}</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <div class="card-header"><h3>Recent Candidates</h3><a href="/candidates" class="btn btn-sm btn-outline">View All</a></div>
          ${data.recent_candidates.length ? `<table><thead><tr><th>Name</th><th>Interview</th><th>Status</th><th>Score</th></tr></thead><tbody>
            ${data.recent_candidates.map(c => `<tr style="cursor:pointer" onclick="window.location.href='/review/${c.id}'">
              <td><strong>${c.first_name} ${c.last_name}</strong><br><span style="font-size:12px;color:#999">${c.email}</span></td>
              <td style="font-size:13px">${c.interview_title}</td>
              <td>${statusBadge(c.status)}</td>
              <td>${c.ai_score ? scoreRing(c.ai_score, 40) : '—'}</td>
            </tr>`).join('')}
          </tbody></table>` : '<div class="empty-state"><p>No candidates yet</p></div>'}
        </div>
        <div class="card">
          <div class="card-header"><h3>Active Interviews</h3><a href="/interviews" class="btn btn-sm btn-outline">View All</a></div>
          ${data.recent_interviews.length ? `<table><thead><tr><th>Interview</th><th>Candidates</th><th>Status</th></tr></thead><tbody>
            ${data.recent_interviews.map(i => `<tr style="cursor:pointer" onclick="window.location.href='/interviews/${i.id}'">
              <td><strong>${i.title}</strong><br><span style="font-size:12px;color:#999">${i.department || ''}</span></td>
              <td>${i.completed_count || 0}/${i.candidate_count || 0}</td>
              <td>${statusBadge(i.status)}</td>
            </tr>`).join('')}
          </tbody></table>` : '<div class="empty-state"><p>No interviews yet</p></div>'}
        </div>
      </div>
      ${data.recent_interviews.length === 0 ? `
      <div class="card" style="text-align:center;padding:40px;margin-top:16px">
        <h3 style="margin-bottom:8px">Get started with sample data</h3>
        <p style="color:#666;margin-bottom:16px">Load sample insurance interviews and candidates to explore ChannelView</p>
        <button class="btn btn-primary" onclick="seedData()">Load Sample Data</button>
      </div>` : ''}
    `;
  } catch (err) {
    content.innerHTML = `<div class="card" style="text-align:center;padding:40px">
      <h3 style="margin-bottom:8px">Welcome to ChannelView!</h3>
      <p style="color:#666;margin-bottom:16px">Load sample data to get started, or create your first interview.</p>
      <button class="btn btn-primary" onclick="seedData()" style="margin-right:8px">Load Sample Data</button>
      <a href="/interviews/new" class="btn btn-secondary">Create Interview</a>
    </div>`;
  }
}

async function seedData() {
  try {
    await api('/api/seed', { method: 'POST' });
    toast('Sample data loaded!', 'success');
    setTimeout(() => location.reload(), 500);
  } catch (err) { toast(err.message, 'error'); }
}

// ==================== INTERVIEWS LIST ====================

async function renderInterviews() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading interviews...</div>';
  const interviews = await api('/api/interviews');
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Interviews</h1><p class="subtitle">Your interview templates</p></div>
      <div class="page-actions"><a href="/interviews/new" class="btn btn-primary">+ New Interview</a></div>
    </div>
    ${interviews.length ? `<div class="card" style="padding:0;overflow:hidden">
      <table><thead><tr><th>Interview</th><th>Position</th><th>Candidates</th><th>Completed</th><th>Status</th><th>Created</th><th></th></tr></thead><tbody>
      ${interviews.map(i => `<tr>
        <td><a href="/interviews/${i.id}" style="color:var(--text);text-decoration:none"><strong>${i.title}</strong></a></td>
        <td>${i.position || '—'}</td>
        <td>${i.candidate_count || 0}</td>
        <td>${i.completed_count || 0}</td>
        <td>${statusBadge(i.status)}</td>
        <td style="font-size:13px">${formatDate(i.created_at)}</td>
        <td><a href="/interviews/${i.id}" class="btn btn-sm btn-outline">View</a></td>
      </tr>`).join('')}
      </tbody></table>
    </div>` : `<div class="empty-state"><div class="icon">🎥</div><h3>No interviews yet</h3><p>Create your first interview template to start screening candidates.</p><a href="/interviews/new" class="btn btn-primary">+ New Interview</a></div>`}
  `;
}

// ==================== QUESTION BANK ====================

const QUESTION_BANK = [
  // ---- SALES & PERSUASION ----
  { category: 'Sales & Persuasion', icon: '💰', text: 'Describe a time you successfully persuaded someone to purchase a policy after they initially said no. What objections did they raise and how did you overcome them?', tags: ['sales','objections','closing'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'Walk me through your process for building a pipeline and generating new leads. How consistent are you with prospecting activities?', tags: ['sales','pipeline','prospecting'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'Describe a sale you lost to a competitor. What did they do better, and what did you learn from it?', tags: ['sales','competition','self-improvement'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'Tell me about your biggest sale. What made it successful and how did you structure the deal?', tags: ['sales','achievement','deal-structure'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'How do you handle rejection? Give me an example of a prospect who shut you down and how you responded.', tags: ['sales','resilience','rejection'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'How do you approach cross-selling or upselling to existing clients without being pushy?', tags: ['sales','cross-sell','ethics'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'Tell me about a time you had to close a sale under pressure. What approach did you use and what was the outcome?', tags: ['sales','pressure','closing'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'How do you use data or metrics to improve your sales performance? Give a specific example.', tags: ['sales','analytics','performance'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'What is your approach when a prospect says they need to think about it? Walk me through your follow-up strategy.', tags: ['sales','follow-up','persistence'] },
  { category: 'Sales & Persuasion', icon: '💰', text: 'Describe a time you had to sell a product you were less familiar with. How did you prepare and what was the result?', tags: ['sales','adaptability','learning'] },

  // ---- PRODUCT KNOWLEDGE ----
  { category: 'Product Knowledge', icon: '📋', text: 'Explain the difference between term life and whole life insurance. When would you recommend each to a client?', tags: ['product','life-insurance','term-vs-whole'] },
  { category: 'Product Knowledge', icon: '📋', text: 'How would you explain deductibles, co-pays, and out-of-pocket maximums to a client who has never had health insurance?', tags: ['product','health','simplification'] },
  { category: 'Product Knowledge', icon: '📋', text: 'Walk me through the key differences between group health insurance and individual plans. When would you recommend each?', tags: ['product','group-vs-individual','ACA'] },
  { category: 'Product Knowledge', icon: '📋', text: 'How would you explain the difference between a PPO, HMO, and EPO plan to a prospective client?', tags: ['product','health','plan-types'] },
  { category: 'Product Knowledge', icon: '📋', text: 'Explain how a health savings account works and why it matters for certain clients.', tags: ['product','HSA','tax-advantage'] },
  { category: 'Product Knowledge', icon: '📋', text: 'What are the main exclusions or limitations a client might overlook when reading their policy? How do you address that?', tags: ['product','exclusions','client-education'] },
  { category: 'Product Knowledge', icon: '📋', text: 'Walk me through the key provisions in a disability insurance policy that matter most to clients.', tags: ['product','disability','details'] },
  { category: 'Product Knowledge', icon: '📋', text: 'How do you stay current with regulatory changes in health insurance? Give me a recent example of something you learned.', tags: ['product','regulation','continuing-education'] },
  { category: 'Product Knowledge', icon: '📋', text: 'What questions do you always ask a client first to determine their actual insurance needs?', tags: ['product','needs-analysis','consultative'] },
  { category: 'Product Knowledge', icon: '📋', text: 'Explain how voluntary worksite benefits like critical illness, accident, and group whole life complement a core benefits package.', tags: ['product','voluntary','worksite'] },

  // ---- COMPLIANCE & ETHICS ----
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'If a client asked you to stretch the truth on an application to qualify for coverage, what would you do?', tags: ['ethics','compliance','integrity'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'Tell me about a time you turned down a sale or recommended a different product because it was not right for the client.', tags: ['ethics','client-first','honesty'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'How do you balance earning a commission with recommending what is truly best for the client?', tags: ['ethics','commission','fiduciary'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'If your manager asked you to use a sales tactic you were not comfortable with, what would you do?', tags: ['ethics','leadership','boundaries'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'Describe your understanding of the regulations that govern insurance sales. What are the key rules you follow daily?', tags: ['compliance','regulation','licensing'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'Walk me through how you maintain client confidentiality and protect sensitive personal information.', tags: ['compliance','data-protection','privacy'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'Have you ever made a mistake in how you presented a policy to a client? How did you handle it?', tags: ['ethics','accountability','mistakes'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'How would you handle a situation where you suspect a client is filing a fraudulent claim?', tags: ['ethics','fraud','judgment'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'A client admits they misrepresented information on their application years ago. What do you do?', tags: ['ethics','compliance','disclosure'] },
  { category: 'Compliance & Ethics', icon: '⚖️', text: 'Tell me about your experience with continuing education and compliance training. What was the most important lesson?', tags: ['compliance','CE','training'] },

  // ---- CLIENT RELATIONSHIPS ----
  { category: 'Client Relationships', icon: '🤝', text: 'Tell me about a time you had an upset or angry client. What caused the issue and how did you resolve it?', tags: ['client','conflict','de-escalation'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Describe your approach to following up with clients after a sale. How often and through what channels?', tags: ['client','retention','follow-up'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Give me an example of when you went above and beyond for a client. What drove you to do it?', tags: ['client','service','above-and-beyond'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Tell me about a client relationship you have built over multiple years. How did you maintain that trust?', tags: ['client','trust','long-term'] },
  { category: 'Client Relationships', icon: '🤝', text: 'How do you communicate with clients who have limited financial literacy or English as a second language?', tags: ['client','accessibility','communication'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Walk me through how you would help a client during the claims process. What role do you play?', tags: ['client','claims','advocacy'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Tell me about a time you had to deliver bad news to a client, such as a coverage denial or rate increase. How did you handle it?', tags: ['client','difficult-conversations','empathy'] },
  { category: 'Client Relationships', icon: '🤝', text: 'How do you know if a client is truly satisfied with the service they have received from you?', tags: ['client','satisfaction','feedback'] },
  { category: 'Client Relationships', icon: '🤝', text: 'Describe a time when a client gave you negative feedback. How did you respond and what changed?', tags: ['client','feedback','growth'] },
  { category: 'Client Relationships', icon: '🤝', text: 'How do you prioritize when multiple clients need your help at the same time?', tags: ['client','prioritization','time-management'] },

  // ---- SCENARIO-BASED ----
  { category: 'Scenario-Based', icon: '🎯', text: 'A client calls with a pre-existing condition not disclosed on their application. The carrier wants to rescind coverage. What do you do?', tags: ['scenario','compliance','advocacy'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'Your top client finds a better rate elsewhere and asks why they should not switch. What is your response?', tags: ['scenario','retention','value-proposition'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'A prospect tells you they cannot afford the policy you recommended. Walk me through your next steps.', tags: ['scenario','affordability','problem-solving'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'You realize you made an error in a client policy that will cost your firm money to fix. How do you handle it?', tags: ['scenario','accountability','integrity'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'A client wants to decrease their coverage right before a major life event. What questions do you ask?', tags: ['scenario','needs-analysis','risk-awareness'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'You are behind on quota and have a client who probably needs less coverage than they currently have. Do you recommend reducing it?', tags: ['scenario','ethics','pressure'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'You discover a competitor is bad-mouthing your firm to a prospect. How do you respond?', tags: ['scenario','professionalism','competition'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'A long-time client asks you to recommend coverage they almost certainly do not need. Do you sell it? Why or why not?', tags: ['scenario','ethics','needs-analysis'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'During open enrollment, you have 40 clients to service in two weeks. Walk me through how you manage the workload.', tags: ['scenario','OEP','time-management'] },
  { category: 'Scenario-Based', icon: '🎯', text: 'A small business owner asks you to put together a voluntary benefits package for 30 employees. Where do you start?', tags: ['scenario','group-benefits','worksite'] },

  // ---- CULTURE & CHARACTER ----
  { category: 'Culture & Character', icon: '🌟', text: 'Tell me about yourself. What should I know about you that would not be in your resume?', tags: ['culture','character','personal'] },
  { category: 'Culture & Character', icon: '🌟', text: 'Why did you choose insurance as a career? What genuinely attracts you to this industry?', tags: ['culture','motivation','career'] },
  { category: 'Culture & Character', icon: '🌟', text: 'Describe your ideal work environment and team. What kind of culture do you thrive in?', tags: ['culture','teamwork','environment'] },
  { category: 'Culture & Character', icon: '🌟', text: 'What does integrity mean to you? Give me an example of when you chose integrity over personal benefit.', tags: ['culture','integrity','values'] },
  { category: 'Culture & Character', icon: '🌟', text: 'How do you stay motivated when sales are slow or you are facing constant rejection?', tags: ['culture','resilience','motivation'] },
  { category: 'Culture & Character', icon: '🌟', text: 'What are your career goals for the next three to five years? How does this role fit into your plan?', tags: ['culture','ambition','career-planning'] },
  { category: 'Culture & Character', icon: '🌟', text: 'Describe a time you disagreed with a manager or colleague. How did you handle it?', tags: ['culture','conflict','maturity'] },
  { category: 'Culture & Character', icon: '🌟', text: 'Tell me about your involvement in your community or any volunteer work. Why is it important to you?', tags: ['culture','community','character'] },
  { category: 'Culture & Character', icon: '🌟', text: 'Tell me about a time you failed at something. What did you learn and what would you do differently?', tags: ['culture','failure','growth-mindset'] },
  { category: 'Culture & Character', icon: '🌟', text: 'How do you handle competing priorities and limited time? Walk me through how you decide what to focus on.', tags: ['culture','prioritization','work-ethic'] },

  // ---- LEADERSHIP & TEAM ----
  { category: 'Leadership & Team', icon: '👥', text: 'Tell me about a time you mentored or helped a colleague succeed. What did you do and what was the outcome?', tags: ['leadership','mentoring','teamwork'] },
  { category: 'Leadership & Team', icon: '👥', text: 'Describe your experience with team selling or collaboration. How do you contribute to a team\'s success?', tags: ['leadership','collaboration','team-selling'] },
  { category: 'Leadership & Team', icon: '👥', text: 'How do you handle competition within your own team? Do you see teammates as competitors or collaborators?', tags: ['leadership','competition','team-dynamics'] },
  { category: 'Leadership & Team', icon: '👥', text: 'Describe your experience working with support staff or operations teams. How do you partner with them effectively?', tags: ['leadership','cross-functional','operations'] },
  { category: 'Leadership & Team', icon: '👥', text: 'Tell me about a time you had to adapt to a significant organizational change. How did you respond?', tags: ['leadership','adaptability','change'] },
  { category: 'Leadership & Team', icon: '👥', text: 'Have you ever led a team or managed other people? Describe your leadership approach.', tags: ['leadership','management','style'] },

  // ---- MEDICARE & SENIOR MARKETS ----
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'What is your experience with Medicare Supplement and Medicare Advantage products?', tags: ['medicare','senior','product-knowledge'] },
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'How do you approach the Annual Enrollment Period differently from Open Enrollment?', tags: ['medicare','AEP','OEP'] },
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'Describe how you would explain the difference between Medigap and Medicare Advantage to a confused senior.', tags: ['medicare','explanation','simplification'] },
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'What compliance considerations are top of mind when selling Medicare products?', tags: ['medicare','compliance','CMS'] },
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'How do you generate leads and build your book of business in the senior market?', tags: ['medicare','leads','prospecting'] },
  { category: 'Medicare & Senior Markets', icon: '🏥', text: 'Walk me through the T65 process and how you identify and reach out to prospects aging into Medicare.', tags: ['medicare','T65','outreach'] },

  // ---- ACA & HEALTH MARKETPLACE ----
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'Walk us through your experience with ACA marketplace plans and the enrollment process.', tags: ['ACA','marketplace','enrollment'] },
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'How do you handle a client frustrated with their premium increase at renewal?', tags: ['ACA','premium','client-retention'] },
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'Describe your approach during Open Enrollment Period. How do you manage high volume efficiently?', tags: ['ACA','OEP','time-management'] },
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'What CRM or agency management systems have you used, and how do they improve your workflow?', tags: ['ACA','technology','AMS'] },
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'How do you help clients who lose coverage mid-year understand their Special Enrollment Period options?', tags: ['ACA','SEP','coverage-gap'] },
  { category: 'ACA & Health Marketplace', icon: '🏛️', text: 'Explain how premium tax credits and cost-sharing reductions work. How do you help clients maximize their subsidies?', tags: ['ACA','subsidies','APTC'] },

  // ---- GROUP BENEFITS & WORKSITE ----
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'Walk me through your experience selling group benefits to employers. What size companies have you worked with?', tags: ['group','employer','experience'] },
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'How would you present a voluntary benefits package to an HR director who says their employees cannot afford additional payroll deductions?', tags: ['group','voluntary','objections'] },
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'Describe your experience running open enrollment meetings for employer groups. What makes an enrollment successful?', tags: ['group','enrollment','presentation'] },
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'How do you approach renewals when an employer group is facing a significant rate increase?', tags: ['group','renewals','retention'] },
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'What is your experience with ICHRA, QSEHRA, or other health reimbursement arrangements?', tags: ['group','ICHRA','HRA'] },
  { category: 'Group Benefits & Worksite', icon: '🏢', text: 'Tell me about your approach to building relationships with HR professionals and business owners as long-term clients.', tags: ['group','relationships','B2B'] },

  // ---- TECHNOLOGY & PROCESS ----
  { category: 'Technology & Process', icon: '💻', text: 'What insurance technology platforms or quoting tools are you comfortable using? How do they help you serve clients better?', tags: ['technology','tools','efficiency'] },
  { category: 'Technology & Process', icon: '💻', text: 'How do you use a CRM to manage your pipeline and stay organized during busy enrollment periods?', tags: ['technology','CRM','organization'] },
  { category: 'Technology & Process', icon: '💻', text: 'Describe your experience with virtual or remote client meetings. How do you build rapport digitally?', tags: ['technology','remote','virtual-sales'] },
  { category: 'Technology & Process', icon: '💻', text: 'How do you use social media or digital marketing to generate leads or build your personal brand as an agent?', tags: ['technology','social-media','marketing'] },
  { category: 'Technology & Process', icon: '💻', text: 'Walk me through your typical workflow from initial contact to policy delivery. What does your process look like?', tags: ['technology','process','workflow'] },

  // ---- SALES SCREENING: DRIVE & MOTIVATION ----
  { category: 'Drive & Motivation', icon: '🔥', text: 'What is the most ambitious goal you have ever set for yourself, and what did you do to achieve it?', tags: ['screening','drive','achievement'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'Tell me about a time you exceeded your sales target. What specifically did you do differently than your peers?', tags: ['screening','drive','overachievement'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'What motivates you more: the money, the competition, or helping people solve problems? Be honest.', tags: ['screening','motivation','self-awareness'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'Describe a period where you were not hitting your numbers. What did you do about it and what was the result?', tags: ['screening','drive','adversity'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'When was the last time you went significantly above and beyond what was expected of you at work? What drove you to do it?', tags: ['screening','initiative','effort'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'What does winning look like to you in a sales role? Paint me a picture of your best month.', tags: ['screening','drive','vision'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'If I called your last manager right now and asked them to describe your work ethic in three words, what would they say?', tags: ['screening','work-ethic','reference'] },
  { category: 'Drive & Motivation', icon: '🔥', text: 'Tell me about something you taught yourself outside of work. Why did you pursue it?', tags: ['screening','curiosity','self-starter'] },

  // ---- SALES SCREENING: REJECTION & RESILIENCE ----
  { category: 'Rejection & Resilience', icon: '💪', text: 'Tell me about the worst day you have ever had in sales. What happened and how did you bounce back?', tags: ['screening','resilience','adversity'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'How many nos can you take in a row before it starts to affect your energy? What do you do when it happens?', tags: ['screening','rejection','mental-toughness'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'Describe a deal you invested significant time in that fell apart at the last minute. How did you handle it?', tags: ['screening','lost-deal','resilience'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'Tell me about a time you received harsh criticism from a client or manager. What did you do with that feedback?', tags: ['screening','criticism','growth'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'What is your honest emotional reaction when a prospect ghosts you after multiple follow-ups? How do you process it?', tags: ['screening','ghosting','emotional-intelligence'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'Have you ever been put on a performance improvement plan or been at risk of losing your job? What happened and what did you learn?', tags: ['screening','adversity','accountability'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'Tell me about a time you failed publicly. How did you recover and what did it teach you about yourself?', tags: ['screening','failure','character'] },
  { category: 'Rejection & Resilience', icon: '💪', text: 'When everything is going wrong in a sales cycle, what is the first thing you do?', tags: ['screening','problem-solving','composure'] },

  // ---- SALES SCREENING: PROSPECTING & PIPELINE ----
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'Walk me through exactly how you would build a pipeline from scratch in a brand new territory with zero existing accounts.', tags: ['screening','prospecting','pipeline-building'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'What is your daily prospecting routine? Be specific about activities, volume, and channels.', tags: ['screening','prospecting','discipline'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'How do you research a prospect before reaching out? Walk me through your preparation for a cold outreach.', tags: ['screening','research','preparation'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'What is the most creative way you have ever generated a lead or gotten a meeting with a hard-to-reach prospect?', tags: ['screening','creativity','prospecting'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'How do you decide which prospects to prioritize and which to deprioritize? What criteria do you use?', tags: ['screening','qualification','prioritization'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'Describe a time you turned a cold lead into a closed deal. What was your approach from first touch to close?', tags: ['screening','cold-outreach','full-cycle'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'How do you keep your pipeline healthy? What warning signs tell you your pipeline is in trouble?', tags: ['screening','pipeline-management','forecasting'] },
  { category: 'Prospecting & Pipeline', icon: '🎣', text: 'What percentage of your business comes from referrals, and what do you specifically do to generate them?', tags: ['screening','referrals','networking'] },

  // ---- SALES SCREENING: CLOSING & NEGOTIATION ----
  { category: 'Closing & Negotiation', icon: '🤑', text: 'Tell me about the most difficult deal you have ever closed. What made it hard and how did you get it across the finish line?', tags: ['screening','closing','complex-sale'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'When a prospect says your price is too high, what do you do? Walk me through your response step by step.', tags: ['screening','price-objection','negotiation'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'Describe a time you had to negotiate with multiple stakeholders or decision-makers. How did you manage each person?', tags: ['screening','stakeholders','complex-sale'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'Have you ever walked away from a deal? What was the situation and why did you make that decision?', tags: ['screening','integrity','deal-qualification'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'What buying signals do you look for that tell you a prospect is ready to close?', tags: ['screening','buying-signals','intuition'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'Tell me about a time you turned a competitor\'s customer into yours. What was your strategy?', tags: ['screening','competitive-displacement','strategy'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'What is the longest sales cycle you have ever worked? How did you stay persistent and keep momentum?', tags: ['screening','patience','long-cycle'] },
  { category: 'Closing & Negotiation', icon: '🤑', text: 'If a prospect says they need to think about it, what do you do next? Be specific.', tags: ['screening','stall-objection','follow-up'] },

  // ---- SALES SCREENING: COACHABILITY ----
  { category: 'Coachability', icon: '📖', text: 'Tell me about a time a manager or mentor gave you feedback that fundamentally changed how you sell. What was it?', tags: ['screening','coachability','feedback'] },
  { category: 'Coachability', icon: '📖', text: 'What is the last skill you deliberately worked to improve? What steps did you take?', tags: ['screening','growth','self-improvement'] },
  { category: 'Coachability', icon: '📖', text: 'Describe a time you had to completely change your approach or strategy mid-deal. What triggered the change?', tags: ['screening','adaptability','learning'] },
  { category: 'Coachability', icon: '📖', text: 'If I watched you on a sales call and told you three things you did wrong, how would you respond?', tags: ['screening','coachability','ego'] },
  { category: 'Coachability', icon: '📖', text: 'What sales books, podcasts, or thought leaders have influenced how you sell? What specifically did you apply?', tags: ['screening','learning','professional-development'] },
  { category: 'Coachability', icon: '📖', text: 'Tell me about a sales methodology or framework you use. How did you learn it and how has it helped you?', tags: ['screening','methodology','structured-selling'] },
  { category: 'Coachability', icon: '📖', text: 'When you lose a deal, what is your process for figuring out what went wrong?', tags: ['screening','self-reflection','improvement'] },
  { category: 'Coachability', icon: '📖', text: 'If you could go back and coach your first-year-in-sales self, what would you tell yourself?', tags: ['screening','self-awareness','growth'] },

  // ---- SALES SCREENING: COMPETITIVE NATURE ----
  { category: 'Competitive Nature', icon: '🏆', text: 'Are you competitive? Give me a real example that proves it, not just in sales but in any part of your life.', tags: ['screening','competitive','character'] },
  { category: 'Competitive Nature', icon: '🏆', text: 'Tell me about a time you were ranked against your peers. Where did you finish and how did you feel about it?', tags: ['screening','ranking','competitiveness'] },
  { category: 'Competitive Nature', icon: '🏆', text: 'What do you do when a colleague on your team is outperforming you? Walk me through your thought process.', tags: ['screening','competition','response'] },
  { category: 'Competitive Nature', icon: '🏆', text: 'Describe the most competitive environment you have ever worked in. Did you thrive or struggle? Why?', tags: ['screening','environment','self-awareness'] },
  { category: 'Competitive Nature', icon: '🏆', text: 'Outside of work, what do you compete in or care about winning at?', tags: ['screening','character','drive'] },
  { category: 'Competitive Nature', icon: '🏆', text: 'Tell me about a contest or incentive you won at work. What did you do to win it?', tags: ['screening','achievement','tactics'] },

  // ---- SALES SCREENING: COMMUNICATION & PRESENCE ----
  { category: 'Communication & Presence', icon: '🎤', text: 'You have 60 seconds. Sell me on why I should hire you over every other candidate for this role.', tags: ['screening','pitch','confidence'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'Tell me a story about yourself that is not on your resume. Something that reveals who you really are.', tags: ['screening','storytelling','authenticity'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'Explain something complex from your previous job as if you were talking to someone with zero industry knowledge.', tags: ['screening','simplification','communication'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'What do people misunderstand about you when they first meet you? How do you handle that?', tags: ['screening','self-awareness','first-impression'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'If you had to deliver a two-minute presentation right now on any topic of your choice, what would you talk about and why?', tags: ['screening','presentation','confidence'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'Tell me about a time your communication style cost you a deal or damaged a relationship. What did you learn?', tags: ['screening','communication','self-improvement'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'How do you build rapport with someone in the first two minutes of a conversation? What do you do specifically?', tags: ['screening','rapport','relationship-building'] },
  { category: 'Communication & Presence', icon: '🎤', text: 'Describe a time you had to persuade someone who was skeptical or resistant. What was your approach?', tags: ['screening','persuasion','influence'] },

  // ---- SALES SCREENING: PROBLEM SOLVING & STRATEGY ----
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'You just started a new sales role and your territory has zero pipeline. It is day one. Walk me through your first 30 days.', tags: ['screening','strategy','ramp-up'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'Tell me about a time you had to sell a product or service that was not the cheapest option. How did you win on value?', tags: ['screening','value-selling','strategy'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'A client loves your product but their budget was just cut by 40 percent. How do you save the deal?', tags: ['screening','creative-problem-solving','budget'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'You have five prospects at different stages. One is ready to close, one is going cold, and three are mid-funnel. How do you prioritize your week?', tags: ['screening','prioritization','time-management'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'Describe a situation where you identified an opportunity that nobody else saw. What did you do with it?', tags: ['screening','opportunity','initiative'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'How do you approach selling to a prospect who had a bad experience with your company or a similar product before?', tags: ['screening','objection-handling','trust-building'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'What is the first thing you do when you realize a deal is at risk of falling through?', tags: ['screening','deal-rescue','instinct'] },
  { category: 'Problem Solving & Strategy', icon: '🧠', text: 'Tell me about a time you had to get creative to hit a deadline or quota. What unconventional approach did you take?', tags: ['screening','creativity','resourcefulness'] },

  // ---- SALES SCREENING: SELF-AWARENESS & AUTHENTICITY ----
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'What is your biggest weakness as a salesperson? Do not give me a strength disguised as a weakness. Be real.', tags: ['screening','self-awareness','honesty'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'If I asked your last five clients to describe working with you, what would they consistently say?', tags: ['screening','reputation','client-perception'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'What type of sale or client do you struggle with the most? Why?', tags: ['screening','self-awareness','growth-areas'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'Tell me about a time you were wrong about something important at work. How did you handle it?', tags: ['screening','humility','accountability'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'What is one thing about your selling style that you know you need to change but have not yet?', tags: ['screening','honesty','self-improvement'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'Why are you looking for a new role right now? What is the real reason you are considering a change?', tags: ['screening','motivation','honesty'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'If you do not get this role, what is your backup plan?', tags: ['screening','planning','authenticity'] },
  { category: 'Self-Awareness & Authenticity', icon: '🪞', text: 'What kind of manager brings out the best in you, and what kind shuts you down?', tags: ['screening','management-style','self-knowledge'] }
];

const QUESTION_CATEGORIES = [...new Set(QUESTION_BANK.map(q => q.category))];

let qbankActiveCategory = 'all';
let qbankSearch = '';

function getFilteredBankQuestions() {
  return QUESTION_BANK.filter(q => {
    const matchCat = qbankActiveCategory === 'all' || q.category === qbankActiveCategory;
    const matchSearch = !qbankSearch || q.text.toLowerCase().includes(qbankSearch.toLowerCase()) || q.tags.some(t => t.toLowerCase().includes(qbankSearch.toLowerCase()));
    return matchCat && matchSearch;
  });
}

function renderQuestionBank() {
  const el = document.getElementById('question-bank-list');
  if (!el) return;
  const filtered = getFilteredBankQuestions();
  const existingTexts = builderQuestions.map(q => q.text);

  if (filtered.length === 0) {
    el.innerHTML = '<div style="text-align:center;padding:24px;color:#999;font-size:13px">No questions match your search.</div>';
    return;
  }

  el.innerHTML = filtered.map((q, idx) => {
    const added = existingTexts.includes(q.text);
    return `<div style="padding:10px 12px;border-bottom:1px solid #f3f4f6;display:flex;gap:10px;align-items:start;${added ? 'opacity:0.5;' : ''}">
      <span style="font-size:16px;flex-shrink:0;margin-top:2px">${q.icon}</span>
      <div style="flex:1;min-width:0">
        <div style="font-size:13px;line-height:1.4;color:#333">${q.text}</div>
        <div style="margin-top:4px;display:flex;gap:4px;flex-wrap:wrap">
          ${q.tags.slice(0, 3).map(t => `<span style="font-size:10px;background:#f3f4f6;color:#666;padding:1px 6px;border-radius:3px">${t}</span>`).join('')}
        </div>
      </div>
      <button type="button" onclick="addBankQuestion(${QUESTION_BANK.indexOf(q)})" class="btn btn-sm ${added ? 'btn-outline' : 'btn-primary'}" style="flex-shrink:0;font-size:11px;padding:4px 10px" ${added ? 'disabled' : ''}>
        ${added ? 'Added' : '+ Add'}
      </button>
    </div>`;
  }).join('');
}

function addBankQuestion(bankIdx) {
  const q = QUESTION_BANK[bankIdx];
  if (!q) return;
  builderQuestions.push({ text: q.text, thinking_time: null, max_answer_time: null });
  renderBuilderQuestions();
  renderQuestionBank();
  toast('Question added!', 'success');
}

function setQBankCategory(cat) {
  qbankActiveCategory = cat;
  document.querySelectorAll('.qbank-cat-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.cat === cat);
  });
  renderQuestionBank();
}

function onQBankSearch(val) {
  qbankSearch = val;
  renderQuestionBank();
}

// ==================== INTRO VIDEO ====================

let introStream = null;
let introRecorder = null;
let introChunks = [];
let introBlob = null;
let introRecording = false;
let introTimerInterval = null;
let introSeconds = 0;
let introAudioCtx = null;
let introAnalyser = null;
let introMicRaf = null;

async function startIntroRecording() {
  try {
    introStream = await navigator.mediaDevices.getUserMedia({ video: true, audio: true });
    const preview = document.getElementById('intro-preview');
    preview.srcObject = introStream;
    document.getElementById('intro-video-empty').style.display = 'none';
    document.getElementById('intro-video-recorder').style.display = 'block';
    document.getElementById('intro-video-saved').style.display = 'none';

    // Enumerate devices and populate selectors
    const devices = await navigator.mediaDevices.enumerateDevices();
    const camSel = document.getElementById('intro-sel-camera');
    const micSel = document.getElementById('intro-sel-mic');
    camSel.innerHTML = '';
    micSel.innerHTML = '';
    const cams = devices.filter(d => d.kind === 'videoinput');
    const mics = devices.filter(d => d.kind === 'audioinput');
    const activeVideoTrack = introStream.getVideoTracks()[0];
    const activeAudioTrack = introStream.getAudioTracks()[0];
    cams.forEach((d, i) => {
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || ('Camera ' + (i + 1));
      if (activeVideoTrack && activeVideoTrack.getSettings().deviceId === d.deviceId) opt.selected = true;
      camSel.appendChild(opt);
    });
    mics.forEach((d, i) => {
      const opt = document.createElement('option');
      opt.value = d.deviceId;
      opt.textContent = d.label || ('Microphone ' + (i + 1));
      if (activeAudioTrack && activeAudioTrack.getSettings().deviceId === d.deviceId) opt.selected = true;
      micSel.appendChild(opt);
    });

    // Set up mic level meter
    startIntroMicMeter();
  } catch (err) {
    toast('Could not access camera/microphone: ' + err.message, 'error');
  }
}

function startIntroMicMeter() {
  if (introAudioCtx) { try { introAudioCtx.close(); } catch(e){} }
  introAudioCtx = new (window.AudioContext || window.webkitAudioContext)();
  introAnalyser = introAudioCtx.createAnalyser();
  introAnalyser.fftSize = 256;
  const src = introAudioCtx.createMediaStreamSource(introStream);
  src.connect(introAnalyser);
  const buf = new Uint8Array(introAnalyser.frequencyBinCount);
  function tick() {
    introAnalyser.getByteFrequencyData(buf);
    const avg = buf.reduce((a, b) => a + b, 0) / buf.length;
    const el = document.getElementById('intro-mic-level');
    if (el) el.style.width = Math.min(100, avg * 1.5) + '%';
    introMicRaf = requestAnimationFrame(tick);
  }
  tick();
}

function stopIntroMicMeter() {
  if (introMicRaf) { cancelAnimationFrame(introMicRaf); introMicRaf = null; }
  if (introAudioCtx) { try { introAudioCtx.close(); } catch(e){} introAudioCtx = null; }
}

// --- Intro Background Processing ---
let introBgMode = 'none';
let introBgImage = null;
let introSegmenter = null;
let introBgReady = false;
let introRafId = null;

async function initIntroSegmenter() {
  if (introSegmenter) return;
  try {
    const { SelfieSegmentation } = await import('https://cdn.jsdelivr.net/npm/@mediapipe/selfie_segmentation@0.1/selfie_segmentation.js');
    introSegmenter = new SelfieSegmentation({ locateFile: f => `https://cdn.jsdelivr.net/npm/@mediapipe/selfie_segmentation@0.1/${f}` });
    introSegmenter.setOptions({ modelSelection: 1, selfieMode: true });
    introSegmenter.onResults(onIntroSegResults);
    introBgReady = true;
  } catch(e) {
    console.warn('MediaPipe not available for intro backgrounds');
    introBgReady = false;
  }
}

function setIntroBg(mode, el) {
  document.querySelectorAll('#intro-bg-options .intro-bg-opt').forEach(o => o.style.borderColor = 'transparent');
  el.style.borderColor = 'var(--primary)';
  introBgMode = mode;
  introBgImage = null;
  const canvas = document.getElementById('intro-bg-canvas');
  if (mode === 'none') {
    canvas.style.display = 'none';
    if (introRafId) { cancelAnimationFrame(introRafId); introRafId = null; }
  } else {
    initIntroSegmenter().then(() => {
      if (introBgReady) { canvas.style.display = 'block'; if (!introRafId) processIntroFrame(); }
    });
  }
}

function setIntroBgImage(url, el) {
  document.querySelectorAll('#intro-bg-options .intro-bg-opt').forEach(o => o.style.borderColor = 'transparent');
  el.style.borderColor = 'var(--primary)';
  introBgMode = 'image';
  introBgImage = new Image();
  introBgImage.crossOrigin = 'anonymous';
  introBgImage.src = url;
  const canvas = document.getElementById('intro-bg-canvas');
  initIntroSegmenter().then(() => {
    if (introBgReady) { canvas.style.display = 'block'; if (!introRafId) processIntroFrame(); }
  });
}

function uploadIntroBg(input) {
  const file = input.files[0];
  if (!file || !file.type.startsWith('image/')) return;
  const reader = new FileReader();
  reader.onload = function(e) {
    // Create a custom tile
    const container = document.getElementById('intro-bg-options');
    const existing = document.getElementById('intro-custom-bg-tile');
    if (existing) existing.remove();
    const div = document.createElement('div');
    div.className = 'intro-bg-opt';
    div.id = 'intro-custom-bg-tile';
    div.style.cssText = 'width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;overflow:hidden';
    div.innerHTML = `<img src="${e.target.result}" style="width:100%;height:100%;object-fit:cover">`;
    div.onclick = function() { setIntroBgImage(e.target.result, div); };
    container.insertBefore(div, container.lastElementChild);
    setIntroBgImage(e.target.result, div);
  };
  reader.readAsDataURL(file);
}

async function processIntroFrame() {
  if (introBgMode === 'none' || !introBgReady || !introStream) return;
  const video = document.getElementById('intro-preview');
  if (video.readyState >= 2) {
    await introSegmenter.send({ image: video });
  }
  introRafId = requestAnimationFrame(processIntroFrame);
}

function onIntroSegResults(results) {
  const canvas = document.getElementById('intro-bg-canvas');
  if (!canvas || canvas.style.display === 'none') return;
  const ctx = canvas.getContext('2d');
  canvas.width = results.image.width;
  canvas.height = results.image.height;
  ctx.save();
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  ctx.drawImage(results.segmentationMask, 0, 0, canvas.width, canvas.height);
  ctx.globalCompositeOperation = 'source-in';
  ctx.drawImage(results.image, 0, 0, canvas.width, canvas.height);
  ctx.globalCompositeOperation = 'destination-over';
  if (introBgMode === 'blur') {
    ctx.filter = 'blur(12px)';
    ctx.drawImage(results.image, 0, 0, canvas.width, canvas.height);
    ctx.filter = 'none';
  } else if (introBgMode === 'image' && introBgImage && introBgImage.complete) {
    const imgRatio = introBgImage.width / introBgImage.height;
    const canRatio = canvas.width / canvas.height;
    let sx=0, sy=0, sw=introBgImage.width, sh=introBgImage.height;
    if (imgRatio > canRatio) { sw = introBgImage.height * canRatio; sx = (introBgImage.width - sw) / 2; }
    else { sh = introBgImage.width / canRatio; sy = (introBgImage.height - sh) / 2; }
    ctx.drawImage(introBgImage, sx, sy, sw, sh, 0, 0, canvas.width, canvas.height);
  } else {
    ctx.fillStyle = '#222';
    ctx.fillRect(0, 0, canvas.width, canvas.height);
  }
  ctx.restore();
}

function stopIntroBg() {
  if (introRafId) { cancelAnimationFrame(introRafId); introRafId = null; }
  introBgMode = 'none';
  introBgImage = null;
  const canvas = document.getElementById('intro-bg-canvas');
  if (canvas) canvas.style.display = 'none';
}

async function switchIntroCamera() {
  const deviceId = document.getElementById('intro-sel-camera').value;
  if (!introStream || introRecording) return;
  try {
    const newStream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: deviceId } }, audio: { deviceId: { exact: document.getElementById('intro-sel-mic').value } } });
    introStream.getTracks().forEach(t => t.stop());
    introStream = newStream;
    document.getElementById('intro-preview').srcObject = introStream;
    stopIntroMicMeter();
    startIntroMicMeter();
  } catch(e) { toast('Could not switch camera', 'error'); }
}

async function switchIntroMic() {
  const deviceId = document.getElementById('intro-sel-mic').value;
  if (!introStream || introRecording) return;
  try {
    const newStream = await navigator.mediaDevices.getUserMedia({ video: { deviceId: { exact: document.getElementById('intro-sel-camera').value } }, audio: { deviceId: { exact: deviceId } } });
    introStream.getTracks().forEach(t => t.stop());
    introStream = newStream;
    document.getElementById('intro-preview').srcObject = introStream;
    stopIntroMicMeter();
    startIntroMicMeter();
  } catch(e) { toast('Could not switch microphone', 'error'); }
}

function toggleIntroRecording() {
  const btn = document.getElementById('intro-rec-start');
  if (!introRecording) {
    // Start recording — use canvas stream if virtual background is active
    introChunks = [];
    introSeconds = 0;
    const mimeType = MediaRecorder.isTypeSupported('video/webm;codecs=vp9,opus') ? 'video/webm;codecs=vp9,opus' : 'video/webm';
    let recStream = introStream;
    const bgCanvas = document.getElementById('intro-bg-canvas');
    if (introBgMode !== 'none' && bgCanvas && bgCanvas.style.display !== 'none') {
      const canvasStream = bgCanvas.captureStream(30);
      const audioTracks = introStream.getAudioTracks();
      if (audioTracks.length > 0) canvasStream.addTrack(audioTracks[0]);
      recStream = canvasStream;
    }
    introRecorder = new MediaRecorder(recStream, { mimeType });
    introRecorder.ondataavailable = e => { if (e.data.size > 0) introChunks.push(e.data); };
    introRecorder.onstop = () => {
      introBlob = new Blob(introChunks, { type: 'video/webm' });
      showIntroPreview(URL.createObjectURL(introBlob));
    };
    introRecorder.start(1000);
    introRecording = true;
    btn.textContent = '■ Stop Recording';
    btn.style.background = '#dc2626';
    btn.style.borderColor = '#dc2626';
    btn.style.color = '#fff';
    document.getElementById('intro-rec-indicator').style.display = 'block';
    introTimerInterval = setInterval(() => {
      introSeconds++;
      const m = Math.floor(introSeconds / 60);
      const s = introSeconds % 60;
      document.getElementById('intro-rec-timer').textContent = `${m}:${s.toString().padStart(2, '0')}`;
      // Auto-stop at 5 minutes
      if (introSeconds >= 300) toggleIntroRecording();
    }, 1000);
  } else {
    // Stop recording
    introRecorder.stop();
    introRecording = false;
    clearInterval(introTimerInterval);
    document.getElementById('intro-rec-indicator').style.display = 'none';
    btn.textContent = '● Start Recording';
    btn.style.background = '';
    btn.style.borderColor = '';
    btn.style.color = '';
    stopIntroStream();
  }
}

function stopIntroStream() {
  stopIntroMicMeter();
  stopIntroBg();
  if (introStream) {
    introStream.getTracks().forEach(t => t.stop());
    introStream = null;
  }
}

function cancelIntroRecording() {
  if (introRecording) {
    introRecorder.stop();
    introRecording = false;
    clearInterval(introTimerInterval);
  }
  stopIntroStream();
  introBlob = null;
  document.getElementById('intro-video-empty').style.display = 'block';
  document.getElementById('intro-video-recorder').style.display = 'none';
  document.getElementById('intro-video-saved').style.display = 'none';
}

function showIntroPreview(url) {
  document.getElementById('intro-video-empty').style.display = 'none';
  document.getElementById('intro-video-recorder').style.display = 'none';
  document.getElementById('intro-video-saved').style.display = 'block';
  const playback = document.getElementById('intro-playback');
  playback.src = url;
}

function uploadIntroFile(input) {
  const file = input.files[0];
  if (!file) return;
  if (!file.type.startsWith('video/')) {
    toast('Please select a video file', 'error');
    return;
  }
  if (file.size > 250 * 1024 * 1024) {
    toast('File too large. Maximum 250MB.', 'error');
    return;
  }
  introBlob = file;
  showIntroPreview(URL.createObjectURL(file));
  toast('Video loaded! Preview it below.', 'success');
}

function reRecordIntro() {
  introBlob = null;
  document.getElementById('intro-video-saved').style.display = 'none';
  startIntroRecording();
}

function removeIntroVideo() {
  introBlob = null;
  introFromLibraryPath = null;
  stopIntroStream();
  document.getElementById('intro-video-empty').style.display = 'block';
  document.getElementById('intro-video-recorder').style.display = 'none';
  document.getElementById('intro-video-saved').style.display = 'none';
}

// --- Intro Video Library ---
let introFromLibraryPath = null;

async function openIntroLibrary() {
  const modal = document.getElementById('modal-intro-library');
  modal.style.display = 'flex';
  const list = document.getElementById('intro-library-list');
  list.innerHTML = '<div style="text-align:center;padding:20px;color:#999">Loading...</div>';
  try {
    const videos = await api('/api/intro-videos');
    if (videos.length === 0) {
      list.innerHTML = '<div style="text-align:center;padding:32px;color:#999"><div style="font-size:28px;margin-bottom:8px">📚</div><p>No saved intro videos yet.</p><p style="font-size:13px;margin-top:4px">Record or upload an intro video, then click "Save to Library" to reuse it.</p></div>';
      return;
    }
    list.innerHTML = videos.map(v => `
      <div style="display:flex;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid #f3f4f6">
        <div style="width:120px;height:68px;border-radius:8px;overflow:hidden;background:#000;flex-shrink:0">
          <video src="${v.video_path}" style="width:100%;height:100%;object-fit:cover" preload="metadata"></video>
        </div>
        <div style="flex:1;min-width:0">
          <div style="font-weight:600;font-size:14px">${v.name}</div>
          <div style="font-size:12px;color:#999;margin-top:2px">${formatDate(v.created_at)}</div>
        </div>
        <div style="display:flex;gap:6px;flex-shrink:0">
          <button class="btn btn-sm btn-primary" onclick="useLibraryIntro('${v.video_path}')" style="font-size:12px">Use This</button>
          <button class="btn btn-sm btn-outline" onclick="deleteLibraryIntro('${v.id}')" style="font-size:12px;color:#dc2626;border-color:#dc2626" title="Delete">×</button>
        </div>
      </div>
    `).join('');
  } catch (err) {
    list.innerHTML = '<div style="text-align:center;padding:20px;color:#dc2626">Failed to load library</div>';
  }
}

function useLibraryIntro(path) {
  introBlob = null;
  introFromLibraryPath = path;
  showIntroPreview(path);
  document.getElementById('modal-intro-library').style.display = 'none';
  // Hide the save-to-library button since it's already in the library
  const saveBtn = document.getElementById('btn-save-to-lib');
  if (saveBtn) saveBtn.style.display = 'none';
  toast('Intro video selected from library!', 'success');
}

async function saveIntroToLibrary() {
  if (!introBlob) { toast('No video to save', 'error'); return; }
  const name = prompt('Give this intro video a name:', 'General Introduction');
  if (!name) return;
  const fd = new FormData();
  fd.append('video', introBlob, 'intro.webm');
  fd.append('name', name);
  try {
    await fetch('/api/intro-videos', { method: 'POST', body: fd });
    toast('Saved to your library!', 'success');
    const saveBtn = document.getElementById('btn-save-to-lib');
    if (saveBtn) { saveBtn.textContent = 'Saved ✓'; saveBtn.disabled = true; }
  } catch (err) { toast('Failed to save', 'error'); }
}

async function deleteLibraryIntro(id) {
  if (!confirm('Delete this intro video from your library?')) return;
  try {
    await api('/api/intro-videos/' + id, { method: 'DELETE' });
    toast('Deleted', 'success');
    openIntroLibrary(); // Refresh the list
  } catch (err) { toast('Failed to delete', 'error'); }
}

// ==================== INTERVIEW BUILDER ====================

let builderQuestions = [];

async function renderInterviewBuilder() {
  builderQuestions = [];
  introBlob = null;
  introFromLibraryPath = null;
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Create Interview</h1><p class="subtitle">Add your questions and choose recording settings</p></div>
    </div>
    <form id="interviewForm" onsubmit="submitInterview(event)">
      <div style="display:grid;grid-template-columns:5fr 4fr;gap:24px">
        <div>
          <div class="card">
            <h3 style="margin-bottom:16px">Interview Details</h3>
            <div class="form-row">
              <div class="form-group"><label>Interview Title *</label><input type="text" id="iv-title" required placeholder="e.g., Licensed Insurance Agent - P&C"></div>
              <div class="form-group"><label>Position</label><input type="text" id="iv-position" placeholder="e.g., P&C Agent"></div>
            </div>
            <div class="form-row">
              <div class="form-group"><label>Department</label><input type="text" id="iv-dept" placeholder="e.g., Sales"></div>
              <div class="form-group"><label>Brand Color</label><input type="color" id="iv-color" value="#0ace0a" style="height:42px"></div>
            </div>
            <div class="form-group"><label>Description</label><textarea id="iv-desc" placeholder="Brief description of the role and what you're looking for..."></textarea></div>
          </div>
          <div class="card">
            <div class="card-header">
              <div>
                <h3 style="margin:0">Intro Video</h3>
                <p style="font-size:12px;color:#999;margin:2px 0 0">Record or upload a personal welcome video candidates will see before starting</p>
              </div>
            </div>
            <div id="intro-video-area">
              <div id="intro-video-empty" style="text-align:center;padding:24px;border:2px dashed #e5e7eb;border-radius:12px">
                <div style="font-size:32px;margin-bottom:8px">🎬</div>
                <p style="color:#666;font-size:14px;margin-bottom:16px">Add a personal touch — introduce yourself, explain the role, or share what you are looking for</p>
                <div style="display:flex;gap:10px;justify-content:center;flex-wrap:wrap">
                  <button type="button" class="btn btn-primary" onclick="startIntroRecording()" style="font-size:13px">
                    <span style="margin-right:4px">📹</span> Record Video
                  </button>
                  <label class="btn btn-outline" style="font-size:13px;cursor:pointer;margin:0">
                    <span style="margin-right:4px">📁</span> Upload File
                    <input type="file" accept="video/*" onchange="uploadIntroFile(this)" style="display:none">
                  </label>
                  <button type="button" class="btn btn-outline" onclick="openIntroLibrary()" style="font-size:13px">
                    <span style="margin-right:4px">📚</span> My Library
                  </button>
                </div>
              </div>
              <div id="intro-video-recorder" style="display:none">
                <div style="position:relative;border-radius:12px;overflow:hidden;background:#000;aspect-ratio:16/9;margin-bottom:12px">
                  <video id="intro-preview" autoplay muted playsinline style="width:100%;height:100%;object-fit:cover"></video>
                  <canvas id="intro-bg-canvas" style="display:none;width:100%;height:100%;object-fit:cover;position:absolute;top:0;left:0;transform:scaleX(-1)"></canvas>
                  <div id="intro-rec-indicator" style="display:none;position:absolute;top:12px;left:12px;background:#dc2626;color:#fff;font-size:12px;font-weight:700;padding:4px 10px;border-radius:20px;animation:pulse 1s infinite;z-index:2">
                    ● REC <span id="intro-rec-timer">0:00</span>
                  </div>
                </div>
                <div style="margin-bottom:10px">
                  <label style="font-size:11px;color:#999;font-weight:600;display:block;margin-bottom:4px">🖼 Background</label>
                  <div style="display:flex;gap:6px;flex-wrap:wrap;align-items:center" id="intro-bg-options">
                    <div class="intro-bg-opt active" data-mode="none" onclick="setIntroBg('none',this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid var(--primary);background:#222;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;color:#999">None</div>
                    <div class="intro-bg-opt" data-mode="blur" onclick="setIntroBg('blur',this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;background:linear-gradient(135deg,#667eea,#764ba2);display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;color:#fff">Blur</div>
                    <div class="intro-bg-opt" data-mode="img" data-url="https://images.unsplash.com/photo-1497366216548-37526070297c?w=1280&q=80" onclick="setIntroBgImage(this.dataset.url,this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;overflow:hidden"><img src="https://images.unsplash.com/photo-1497366216548-37526070297c?w=80&q=50" style="width:100%;height:100%;object-fit:cover"></div>
                    <div class="intro-bg-opt" data-mode="img" data-url="https://images.unsplash.com/photo-1507842217343-583bb7270b66?w=1280&q=80" onclick="setIntroBgImage(this.dataset.url,this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;overflow:hidden"><img src="https://images.unsplash.com/photo-1507842217343-583bb7270b66?w=80&q=50" style="width:100%;height:100%;object-fit:cover"></div>
                    <div class="intro-bg-opt" data-mode="img" data-url="https://images.unsplash.com/photo-1524758631624-e2822e304c36?w=1280&q=80" onclick="setIntroBgImage(this.dataset.url,this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;overflow:hidden"><img src="https://images.unsplash.com/photo-1524758631624-e2822e304c36?w=80&q=50" style="width:100%;height:100%;object-fit:cover"></div>
                    <div class="intro-bg-opt" data-mode="img" data-url="https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=1280&q=80" onclick="setIntroBgImage(this.dataset.url,this)" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;overflow:hidden"><img src="https://images.unsplash.com/photo-1586023492125-27b2c045efd7?w=80&q=50" style="width:100%;height:100%;object-fit:cover"></div>
                    <label class="intro-bg-opt" style="width:44px;height:30px;border-radius:5px;cursor:pointer;border:2px solid transparent;background:#f3f4f6;display:flex;align-items:center;justify-content:center;font-size:9px;font-weight:600;color:#666" title="Upload your own">
                      + <input type="file" accept="image/*" onchange="uploadIntroBg(this)" style="display:none">
                    </label>
                  </div>
                </div>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px">
                  <div>
                    <label style="font-size:11px;color:#999;font-weight:600;display:block;margin-bottom:4px">📷 Camera</label>
                    <select id="intro-sel-camera" onchange="switchIntroCamera()" style="width:100%;padding:7px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:12px;font-family:inherit"></select>
                  </div>
                  <div>
                    <label style="font-size:11px;color:#999;font-weight:600;display:block;margin-bottom:4px">🎙 Microphone</label>
                    <select id="intro-sel-mic" onchange="switchIntroMic()" style="width:100%;padding:7px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:12px;font-family:inherit"></select>
                  </div>
                </div>
                <div style="height:5px;background:#e5e7eb;border-radius:3px;overflow:hidden;margin-bottom:12px"><div id="intro-mic-level" style="height:100%;width:0;background:var(--primary);transition:width .1s;border-radius:3px"></div></div>
                <div style="display:flex;gap:8px;justify-content:center">
                  <button type="button" id="intro-rec-start" class="btn btn-primary" onclick="toggleIntroRecording()" style="font-size:13px">● Start Recording</button>
                  <button type="button" class="btn btn-outline" onclick="cancelIntroRecording()" style="font-size:13px">Cancel</button>
                </div>
                <p style="font-size:11px;color:#999;text-align:center;margin-top:8px">Up to 5 minutes. We recommend 1-3 minutes — keep it warm, personal, and to the point.</p>
              </div>
              <div id="intro-video-saved" style="display:none">
                <div style="position:relative;border-radius:12px;overflow:hidden;background:#000;aspect-ratio:16/9;margin-bottom:12px">
                  <video id="intro-playback" controls style="width:100%;height:100%;object-fit:cover"></video>
                </div>
                <div style="display:flex;gap:8px;justify-content:center;flex-wrap:wrap">
                  <button type="button" class="btn btn-outline" onclick="reRecordIntro()" style="font-size:13px">Re-record</button>
                  <button type="button" class="btn btn-outline" onclick="saveIntroToLibrary()" style="font-size:13px" id="btn-save-to-lib">
                    <span style="margin-right:3px">📚</span> Save to Library
                  </button>
                  <button type="button" class="btn btn-outline" onclick="removeIntroVideo()" style="font-size:13px;color:#dc2626;border-color:#dc2626">Remove</button>
                </div>
              </div>
              <!-- Intro Video Library Modal -->
              <div class="modal-overlay" id="modal-intro-library" onclick="if(event.target===this)this.classList.remove('show')" style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:none;align-items:center;justify-content:center">
                <div style="background:#fff;border-radius:12px;width:100%;max-width:600px;max-height:80vh;overflow:hidden;display:flex;flex-direction:column">
                  <div style="padding:16px 20px;border-bottom:1px solid #e5e7eb;display:flex;align-items:center;justify-content:space-between">
                    <h3 style="margin:0">My Intro Videos</h3>
                    <button onclick="document.getElementById('modal-intro-library').style.display='none'" style="background:none;border:none;font-size:22px;cursor:pointer;color:#999">×</button>
                  </div>
                  <div id="intro-library-list" style="overflow-y:auto;flex:1;padding:16px 20px"></div>
                </div>
              </div>
            </div>
          </div>
          <div class="card">
            <div class="card-header"><h3>Your Questions <span id="q-count" style="font-size:13px;color:#999;font-weight:400">(0)</span></h3><button type="button" class="btn btn-sm btn-outline" onclick="addBuilderQ()">+ Write Custom</button></div>
            <div id="questions-list"><div style="text-align:center;padding:24px;color:#999;font-size:14px">Browse the Question Library and click <strong>+ Add</strong> to build your interview, or write your own.</div></div>
          </div>
          <div class="card">
            <h3 style="margin-bottom:16px">Recording Settings</h3>
            <div class="form-row" style="grid-template-columns:1fr 1fr 1fr">
              <div class="form-group"><label>Thinking Time (sec)</label><input type="number" id="iv-think" value="30" min="10" max="300"></div>
              <div class="form-group"><label>Max Answer Time (sec)</label><input type="number" id="iv-maxtime" value="120" min="30" max="600"></div>
              <div class="form-group"><label>Max Retakes</label><input type="number" id="iv-retakes" value="1" min="0" max="5"></div>
            </div>
          </div>
          <div class="card">
            <h3 style="margin-bottom:16px">Candidate Messages</h3>
            <div class="form-group"><label>Welcome Message</label><textarea id="iv-welcome" rows="3">Welcome! This interview consists of a few video questions. Take your time and be yourself.</textarea></div>
            <div class="form-group"><label>Thank You Message</label><textarea id="iv-thanks" rows="3">Thank you for completing your interview! We will review your responses and be in touch soon.</textarea></div>
          </div>
          <button type="submit" class="btn btn-primary" style="width:100%;padding:14px" id="iv-submit">Create Interview</button>
        </div>
        <div>
          <div class="card" style="padding:0;position:sticky;top:16px;max-height:calc(100vh - 32px);display:flex;flex-direction:column">
            <div style="padding:16px 16px 0">
              <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
                <h3 style="margin:0">Question Library</h3>
                <span style="font-size:12px;color:#999">${QUESTION_BANK.length} questions</span>
              </div>
              <input type="text" placeholder="Search questions..." oninput="onQBankSearch(this.value)" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;margin-bottom:10px">
              <div style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:12px">
                <button type="button" class="qbank-cat-btn active" data-cat="all" onclick="setQBankCategory('all')" style="padding:3px 10px;font-size:11px;border-radius:20px;border:1px solid #e5e7eb;background:#111;color:#fff;cursor:pointer;font-weight:500">All</button>
                ${QUESTION_CATEGORIES.map(c => {
                  const icon = QUESTION_BANK.find(q => q.category === c)?.icon || '';
                  return `<button type="button" class="qbank-cat-btn" data-cat="${c}" onclick="setQBankCategory('${c}')" style="padding:3px 10px;font-size:11px;border-radius:20px;border:1px solid #e5e7eb;background:#fff;color:#333;cursor:pointer;font-weight:500">${icon} ${c}</button>`;
                }).join('')}
              </div>
            </div>
            <div id="question-bank-list" style="overflow-y:auto;flex:1;min-height:0"></div>
          </div>
        </div>
      </div>
    </form>
  `;
  renderBuilderQuestions();
  renderQuestionBank();
}

function renderBuilderQuestions() {
  const el = document.getElementById('questions-list');
  if (!el) return;
  const countEl = document.getElementById('q-count');
  if (countEl) countEl.textContent = `(${builderQuestions.length})`;

  if (builderQuestions.length === 0) {
    el.innerHTML = '<div style="text-align:center;padding:24px;color:#999;font-size:14px">Browse the Question Library and click <strong>+ Add</strong> to build your interview, or write your own.</div>';
    return;
  }

  el.innerHTML = builderQuestions.map((q, i) => `
    <div style="display:flex;gap:10px;align-items:start;margin-bottom:8px;padding:10px 12px;background:#f9fafb;border-radius:8px;border-left:3px solid var(--primary)">
      <span style="color:var(--primary);font-weight:700;min-width:24px;padding-top:8px;font-size:15px">${i + 1}</span>
      <textarea style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;font-family:inherit;min-height:56px;line-height:1.4;resize:vertical"
        onchange="builderQuestions[${i}].text=this.value">${q.text}</textarea>
      <div style="display:flex;flex-direction:column;gap:4px">
        ${i > 0 ? `<button type="button" onclick="moveBuilderQ(${i},-1)" style="background:none;border:1px solid #e5e7eb;border-radius:4px;cursor:pointer;font-size:12px;padding:2px 6px;color:#666" title="Move up">↑</button>` : ''}
        ${i < builderQuestions.length - 1 ? `<button type="button" onclick="moveBuilderQ(${i},1)" style="background:none;border:1px solid #e5e7eb;border-radius:4px;cursor:pointer;font-size:12px;padding:2px 6px;color:#666" title="Move down">↓</button>` : ''}
        <button type="button" onclick="builderQuestions.splice(${i},1);renderBuilderQuestions();renderQuestionBank()" style="background:none;border:none;color:#dc2626;cursor:pointer;font-size:16px;padding:2px 6px" title="Remove">×</button>
      </div>
    </div>
  `).join('');
}

function moveBuilderQ(idx, dir) {
  const newIdx = idx + dir;
  if (newIdx < 0 || newIdx >= builderQuestions.length) return;
  [builderQuestions[idx], builderQuestions[newIdx]] = [builderQuestions[newIdx], builderQuestions[idx]];
  renderBuilderQuestions();
}

function addBuilderQ() {
  builderQuestions.push({ text: '', thinking_time: null, max_answer_time: null });
  renderBuilderQuestions();
  const textareas = document.querySelectorAll('#questions-list textarea');
  textareas[textareas.length - 1].focus();
}

async function submitInterview(e) {
  e.preventDefault();
  const btn = document.getElementById('iv-submit');
  btn.disabled = true; btn.textContent = 'Creating...';
  try {
    const res = await api('/api/interviews', {
      method: 'POST',
      body: JSON.stringify({
        title: document.getElementById('iv-title').value,
        position: document.getElementById('iv-position').value,
        department: document.getElementById('iv-dept').value,
        description: document.getElementById('iv-desc').value,
        brand_color: document.getElementById('iv-color').value,
        thinking_time: parseInt(document.getElementById('iv-think').value),
        max_answer_time: parseInt(document.getElementById('iv-maxtime').value),
        max_retakes: parseInt(document.getElementById('iv-retakes').value),
        welcome_msg: document.getElementById('iv-welcome').value,
        thank_you_msg: document.getElementById('iv-thanks').value,
        questions: builderQuestions.filter(q => q.text.trim())
      })
    });
    // Upload intro video if one was recorded/uploaded, or set library path
    if (introBlob) {
      btn.textContent = 'Uploading intro video...';
      const fd = new FormData();
      fd.append('video', introBlob, 'intro.webm');
      await fetch(`/api/interviews/${res.id}/intro-video`, { method: 'POST', body: fd });
    } else if (introFromLibraryPath) {
      await api(`/api/interviews/${res.id}`, { method: 'PUT', body: JSON.stringify({ intro_video_path: introFromLibraryPath }) });
    }
    toast('Interview created!', 'success');
    window.location.href = '/interviews/' + res.id;
  } catch (err) {
    toast(err.message, 'error');
    btn.disabled = false; btn.textContent = 'Create Interview';
  }
}

// ==================== INTERVIEW DETAIL ====================

async function renderInterviewDetail() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';
  const iv = await api(`/api/interviews/${EXTRA_ID}`);
  const baseUrl = window.location.origin;
  content.innerHTML = `
    <div class="page-header">
      <div>
        <div style="display:flex;align-items:center;gap:12px"><h1>${iv.title}</h1>${statusBadge(iv.status)}</div>
        <p class="subtitle">${iv.department || ''} ${iv.position ? '· ' + iv.position : ''}</p>
      </div>
      <div class="page-actions">
        <button class="btn btn-outline" onclick="showAddCandidateModal('${iv.id}')">+ Add Candidate</button>
        <button class="btn btn-outline" onclick="cloneInterview('${iv.id}')">📋 Clone</button>
        <button class="btn btn-outline" onclick="showScheduleModal('${iv.id}')">📅 Schedule</button>
        <button class="btn btn-sm ${iv.status === 'active' ? 'btn-outline' : 'btn-primary'}" onclick="toggleInterviewStatus('${iv.id}','${iv.status}')">
          ${iv.status === 'active' ? 'Pause' : 'Activate'}
        </button>
      </div>
    </div>
    <div class="stat-grid" style="grid-template-columns:repeat(6,1fr)">
      <div class="stat-card"><div class="stat-label">Questions</div><div class="stat-value">${iv.questions.length}</div></div>
      <div class="stat-card"><div class="stat-label">Candidates</div><div class="stat-value">${iv.candidates.length}</div></div>
      <div class="stat-card"><div class="stat-label">Completed</div><div class="stat-value">${iv.candidates.filter(c=>c.status==='completed'||c.status==='reviewed'||c.status==='hired').length}</div></div>
      <div class="stat-card"><div class="stat-label">Thinking Time</div><div class="stat-value">${iv.thinking_time}s</div></div>
      <div class="stat-card"><div class="stat-label">Max Answer</div><div class="stat-value">${iv.max_answer_time}s</div></div>
      <div class="stat-card" id="deadline-stat"><div class="stat-label">Deadline</div><div class="stat-value" style="font-size:14px" id="deadline-val">—</div></div>
    </div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
      <div class="card">
        <div class="card-header"><h3>Candidates</h3></div>
        ${iv.candidates.length ? `<table><thead><tr><th>Name</th><th>Email</th><th>Status</th><th>Score</th><th>Interest</th><th>Link</th><th></th></tr></thead><tbody>
          ${iv.candidates.map(c => `<tr>
            <td><strong>${c.first_name} ${c.last_name}</strong></td>
            <td style="font-size:13px">${c.email}</td>
            <td>${statusBadge(c.status)}</td>
            <td>${c.ai_score ? scoreRing(c.ai_score, 36) : '—'}</td>
            <td>${c.interest_rating ? `<span style="display:inline-flex;align-items:center;background:${c.interest_rating>=8?'rgba(10,206,10,.1)':c.interest_rating>=5?'rgba(255,165,0,.1)':'rgba(220,38,38,.1)'};color:${c.interest_rating>=8?'#0ace0a':c.interest_rating>=5?'#f59e0b':'#dc2626'};padding:2px 8px;border-radius:10px;font-size:12px;font-weight:600">${c.interest_rating}/10</span>` : '—'}</td>
            <td><button class="btn btn-sm btn-outline" onclick="copyLink('${baseUrl}/i/${c.token}')" title="Copy interview link">Copy Link</button></td>
            <td><a href="/review/${c.id}" class="btn btn-sm btn-outline">Review</a></td>
          </tr>`).join('')}
        </tbody></table>` : '<div class="empty-state"><p>No candidates added yet.</p><button class="btn btn-primary btn-sm" onclick="showAddCandidateModal(\''+iv.id+'\')">+ Add Candidate</button></div>'}
      </div>
      <div>
        <div class="card">
          <h3 style="margin-bottom:12px">Questions</h3>
          ${iv.questions.map((q, i) => `
            <div style="padding:10px 0;${i < iv.questions.length - 1 ? 'border-bottom:1px solid #f3f4f6' : ''}">
              <span style="color:#999;font-weight:600">${i + 1}.</span> ${q.question_text}
            </div>
          `).join('')}
        </div>
        <div class="card">
          <h3 style="margin-bottom:8px">Description</h3>
          <p style="color:#666;font-size:14px">${iv.description || 'No description'}</p>
        </div>
        <div class="card">
          <h3 style="margin-bottom:4px">Candidate Intro</h3>
          <p style="color:#888;font-size:13px;margin-bottom:14px">Plays before the first question — sets the tone and sells the opportunity</p>
          <div style="display:flex;gap:6px;margin-bottom:14px;border-bottom:1px solid #e5e7eb;padding-bottom:8px">
            <button class="btn btn-sm ${iv.intro_type==='template'||!iv.intro_type||iv.intro_type==='none'?'btn-primary':'btn-outline'}" onclick="showIntroTab('templates','${iv.id}')" id="intro-tab-templates">Choose a Template</button>
            <button class="btn btn-sm ${iv.intro_type==='uploaded'?'btn-primary':'btn-outline'}" onclick="showIntroTab('upload','${iv.id}')" id="intro-tab-upload">Upload Video</button>
            <button class="btn btn-sm btn-outline" onclick="showIntroTab('record','${iv.id}')" id="intro-tab-record" style="opacity:.5;cursor:not-allowed" title="Coming soon">Record Your Own <span style="font-size:10px;background:#f3f4f6;padding:2px 6px;border-radius:4px;margin-left:4px">Soon</span></button>
          </div>
          <div id="intro-panel-templates" class="intro-panel">
            <div id="intro-template-grid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:10px">
              <div style="text-align:center;padding:20px;color:#999;font-size:13px">Loading templates...</div>
            </div>
          </div>
          <div id="intro-panel-upload" class="intro-panel" style="display:none">
            ${iv.intro_video_path && iv.intro_type==='uploaded' ? `
              <div style="border-radius:8px;overflow:hidden;background:#000;aspect-ratio:16/9;margin-bottom:10px">
                <video controls src="${iv.intro_video_path}" style="width:100%;height:100%;object-fit:cover"></video>
              </div>
              <div style="display:flex;gap:6px">
                <label class="btn btn-sm btn-outline" style="cursor:pointer;flex:1;text-align:center">
                  Replace <input type="file" accept="video/*" onchange="replaceDetailIntro('${iv.id}', this)" style="display:none">
                </label>
                <button class="btn btn-sm btn-outline" style="color:#dc2626;border-color:#dc2626;flex:1" onclick="deleteDetailIntro('${iv.id}')">Remove</button>
              </div>
            ` : `
              <div style="text-align:center;padding:20px;border:2px dashed #e5e7eb;border-radius:8px">
                <p style="color:#999;font-size:13px;margin-bottom:10px">Upload a video your candidates will see before the interview starts</p>
                <label class="btn btn-sm btn-primary" style="cursor:pointer">
                  Upload Video <input type="file" accept="video/*" onchange="replaceDetailIntro('${iv.id}', this)" style="display:none">
                </label>
              </div>
            `}
          </div>
          <div id="intro-panel-record" class="intro-panel" style="display:none">
            <div style="text-align:center;padding:20px;border:2px dashed #e5e7eb;border-radius:8px">
              <p style="font-size:28px;margin-bottom:8px">🎥</p>
              <p style="color:#999;font-size:13px">Record your own intro video — coming soon!</p>
              <p style="color:#aaa;font-size:12px;margin-top:4px">You'll be able to record a personal message right from your browser.</p>
            </div>
          </div>
        </div>
        <div class="card">
          <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:8px">
            <h3>Interest Rating</h3>
            <label style="display:flex;align-items:center;gap:6px;font-size:13px;color:#666;cursor:pointer">
              <input type="checkbox" ${iv.interest_rating_enabled!==0?'checked':''} onchange="toggleInterestRating('${iv.id}',this.checked)"> Enabled
            </label>
          </div>
          <p style="color:#888;font-size:13px;margin-bottom:10px">After the last video question, candidates rate their interest 1-10: "How interested are you in learning more about this opportunity?"</p>
          <div style="display:flex;gap:4px;justify-content:center;padding:8px 0">
            ${[1,2,3,4,5,6,7,8,9,10].map(n=>`<div style="width:28px;height:28px;border-radius:50%;background:${n<=7?'#e5e7eb':'#0ace0a'};color:${n<=7?'#666':'#000'};display:flex;align-items:center;justify-content:center;font-size:12px;font-weight:600">${n}</div>`).join('')}
          </div>
          <p style="color:#aaa;font-size:12px;text-align:center;margin-top:6px">Preview of what candidates see</p>
        </div>
      </div>
    </div>
    <!-- Add Candidate Modal -->
    <div class="modal-overlay" id="modal-add-candidate" onclick="if(event.target===this)this.classList.remove('show')">
      <div class="modal">
        <div class="modal-header"><h2>Add Candidate</h2><button class="modal-close" onclick="document.getElementById('modal-add-candidate').classList.remove('show')">×</button></div>
        <form onsubmit="addCandidate(event)">
          <input type="hidden" id="ac-interview" value="${iv.id}">
          <div class="form-row">
            <div class="form-group"><label>First Name *</label><input type="text" id="ac-fname" required></div>
            <div class="form-group"><label>Last Name *</label><input type="text" id="ac-lname" required></div>
          </div>
          <div class="form-group"><label>Email *</label><input type="email" id="ac-email" required></div>
          <div class="form-group"><label>Phone</label><input type="tel" id="ac-phone"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-outline" onclick="document.getElementById('modal-add-candidate').classList.remove('show')">Cancel</button>
            <button type="submit" class="btn btn-primary" id="ac-submit">Add & Generate Link</button>
          </div>
        </form>
      </div>
    </div>
  `;
  loadDeadlineStatus(iv.id);
  // Auto-load intro templates grid
  loadIntroTemplates(iv.id);
}

function showAddCandidateModal() {
  document.getElementById('modal-add-candidate').classList.add('show');
}

async function addCandidate(e) {
  e.preventDefault();
  const btn = document.getElementById('ac-submit');
  btn.disabled = true;
  try {
    const res = await api('/api/candidates', {
      method: 'POST',
      body: JSON.stringify({
        interview_id: document.getElementById('ac-interview').value,
        first_name: document.getElementById('ac-fname').value,
        last_name: document.getElementById('ac-lname').value,
        email: document.getElementById('ac-email').value,
        phone: document.getElementById('ac-phone').value
      })
    });
    const link = `${window.location.origin}/i/${res.token}`;
    toast('Candidate added! Link copied to clipboard.', 'success');
    navigator.clipboard.writeText(link).catch(() => {});
    document.getElementById('modal-add-candidate').classList.remove('show');
    renderInterviewDetail();
  } catch (err) { toast(err.message, 'error'); btn.disabled = false; }
}

async function replaceDetailIntro(interviewId, input) {
  const file = input.files[0];
  if (!file) return;
  if (!file.type.startsWith('video/')) { toast('Please select a video file', 'error'); return; }
  const fd = new FormData();
  fd.append('video', file, file.name);
  try {
    await fetch(`/api/interviews/${interviewId}/intro-video`, { method: 'POST', body: fd });
    toast('Intro video updated!', 'success');
    renderInterviewDetail();
  } catch (err) { toast('Upload failed', 'error'); }
}

async function deleteDetailIntro(interviewId) {
  try {
    await api(`/api/interviews/${interviewId}/intro-video`, { method: 'DELETE' });
    toast('Intro video removed', 'success');
    renderInterviewDetail();
  } catch (err) { toast('Failed to remove', 'error'); }
}

// ======================== CYCLE 35: INTRO TEMPLATES & INTEREST RATING ========================

function showIntroTab(tab, interviewId) {
  ['templates','upload','record'].forEach(t => {
    const panel = document.getElementById('intro-panel-' + t);
    const btn = document.getElementById('intro-tab-' + t);
    if (panel) panel.style.display = t === tab ? 'block' : 'none';
    if (btn && t !== 'record') { btn.className = t === tab ? 'btn btn-sm btn-primary' : 'btn btn-sm btn-outline'; }
  });
  if (tab === 'templates') loadIntroTemplates(interviewId);
}

async function loadIntroTemplates(interviewId) {
  const grid = document.getElementById('intro-template-grid');
  if (!grid) return;
  try {
    const templates = await api('/api/intro-templates');
    // Get current interview to check which template is selected
    const iv = await api('/api/interviews/' + interviewId);
    const selectedId = iv.intro_template_id || '';
    grid.innerHTML = templates.map(t => `
      <div onclick="selectIntroTemplate('${interviewId}','${t.id}')" style="cursor:pointer;border:2px solid ${t.id===selectedId?'#0ace0a':'#e5e7eb'};border-radius:12px;padding:16px;text-align:center;transition:border-color .2s;background:${t.id===selectedId?'rgba(10,206,10,.05)':'#fff'}">
        <div style="font-size:32px;margin-bottom:8px">${t.thumbnail_emoji || '👋'}</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">${t.name}</div>
        <div style="font-size:12px;color:#888;line-height:1.4">${t.description || ''}</div>
        <div style="margin-top:8px;font-size:11px;color:${t.id===selectedId?'#0ace0a':'#aaa'}">${t.id===selectedId?'✓ Selected':'~'+t.duration_seconds+'s'}</div>
      </div>
    `).join('') + `
      <div onclick="selectIntroTemplate('${interviewId}','')" style="cursor:pointer;border:2px solid ${!selectedId||selectedId===''?'#0ace0a':'#e5e7eb'};border-radius:12px;padding:16px;text-align:center;transition:border-color .2s;background:${!selectedId?'rgba(10,206,10,.05)':'#fff'}">
        <div style="font-size:32px;margin-bottom:8px">⏭️</div>
        <div style="font-size:14px;font-weight:600;margin-bottom:4px">No Intro</div>
        <div style="font-size:12px;color:#888;line-height:1.4">Skip the intro — go straight to questions</div>
        <div style="margin-top:8px;font-size:11px;color:${!selectedId?'#0ace0a':'#aaa'}">${!selectedId?'✓ Selected':''}</div>
      </div>
    `;
  } catch (err) { grid.innerHTML = '<p style="color:#999;font-size:13px">Failed to load templates</p>'; }
}

async function selectIntroTemplate(interviewId, templateId) {
  try {
    await api('/api/interviews/' + interviewId + '/intro-template', {
      method: 'PUT',
      body: JSON.stringify({ template_id: templateId || null })
    });
    toast(templateId ? 'Intro template selected!' : 'Intro removed', 'success');
    loadIntroTemplates(interviewId);
  } catch (err) { toast('Failed to set intro', 'error'); }
}

async function previewIntroTemplate(htmlPath) {
  const w = window.open(htmlPath, '_blank', 'width=800,height=500');
  if (w) w.focus();
}

async function toggleInterestRating(interviewId, enabled) {
  try {
    await api('/api/interviews/' + interviewId, {
      method: 'PUT',
      body: JSON.stringify({ interest_rating_enabled: enabled ? 1 : 0 })
    });
    toast(enabled ? 'Interest rating enabled' : 'Interest rating disabled', 'success');
  } catch (err) { toast('Failed to update', 'error'); }
}

async function toggleInterviewStatus(id, current) {
  const newStatus = current === 'active' ? 'paused' : 'active';
  await api(`/api/interviews/${id}`, { method: 'PUT', body: JSON.stringify({ status: newStatus }) });
  toast(`Interview ${newStatus}`, 'success');
  renderInterviewDetail();
}

function copyLink(url) {
  navigator.clipboard.writeText(url).then(() => toast('Link copied!', 'success')).catch(() => toast('Failed to copy', 'error'));
}

async function cloneInterview(id) {
  const title = prompt('Title for the cloned interview (leave blank for default):');
  try {
    const body = title ? { title } : {};
    const res = await api(`/api/interviews/${id}/clone`, {
      method: 'POST', body: JSON.stringify(body), headers: { 'Content-Type': 'application/json' }
    });
    toast('Interview cloned! Redirecting...', 'success');
    setTimeout(() => { window.location.href = `/interviews/${res.id}`; }, 500);
  } catch(e) { toast(e.message, 'error'); }
}

function showScheduleModal(interviewId) {
  const old = document.getElementById('schedule-modal');
  if (old) old.remove();
  const modal = document.createElement('div');
  modal.id = 'schedule-modal';
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)document.getElementById('schedule-modal').remove()">
      <div style="background:#fff;border-radius:12px;padding:24px;width:400px;max-width:90vw">
        <h3 style="margin-bottom:16px">Set Interview Deadline</h3>
        <div class="form-group"><label>Expiration Date & Time</label><input type="datetime-local" id="sched-expires" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px"></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-outline" onclick="document.getElementById('schedule-modal').remove()">Cancel</button>
          <button class="btn btn-primary" onclick="setSchedule('${interviewId}')">Set Deadline</button>
        </div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

async function setSchedule(interviewId) {
  const val = document.getElementById('sched-expires').value;
  if (!val) { toast('Select a date and time', 'error'); return; }
  try {
    await api(`/api/interviews/${interviewId}/schedule`, {
      method: 'PUT', body: JSON.stringify({ expires_at: new Date(val).toISOString() })
    });
    toast('Deadline set!', 'success');
    document.getElementById('schedule-modal').remove();
    loadDeadlineStatus(interviewId);
  } catch(e) { toast(e.message, 'error'); }
}

async function loadDeadlineStatus(interviewId) {
  try {
    const d = await api(`/api/interviews/${interviewId}/deadline-status`);
    const el = document.getElementById('deadline-val');
    if (!el) return;
    if (d.expires_at) {
      const hrs = Math.round(d.hours_remaining);
      if (d.is_expired) { el.innerHTML = '<span style="color:#dc2626">Expired</span>'; }
      else { el.innerHTML = `<span style="color:${hrs<24?'#d97706':'#059669'}">${hrs}h left</span>`; }
    } else { el.textContent = 'None'; }
  } catch(e) {}
}

// ==================== CANDIDATES PIPELINE ====================

let candidateViewMode = 'table'; // 'table' or 'kanban'
let candidateSearch = '';
let candidateStatus = '';
let candidateSort = 'created_at';
let candidateSortDir = 'desc';
let candidatePage = 1;
let candidatePerPage = 25;
let selectedCandidates = new Set();

async function renderCandidates() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading candidates...</div>';
  const params = new URLSearchParams();
  if (candidateSearch) params.set('search', candidateSearch);
  if (candidateStatus) params.set('status', candidateStatus);
  params.set('sort', candidateSort);
  params.set('dir', candidateSortDir);
  params.set('page', candidatePage);
  params.set('per_page', candidatePerPage);

  const data = await api(`/api/candidates?${params}`);
  const candidates = data.candidates || data;
  const total = data.total || candidates.length;
  const pages_count = data.pages || 1;

  // Also load all tags for filter
  let allTags = [];
  try { allTags = await api('/api/tags'); } catch(e) {}

  selectedCandidates.clear();

  const columns = [
    { key: 'invited', label: 'Invited', color: '#2563eb' },
    { key: 'in_progress', label: 'In Progress', color: '#d97706' },
    { key: 'completed', label: 'Completed', color: '#0ace0a' },
    { key: 'reviewed', label: 'Reviewed', color: '#059669' },
    { key: 'hired', label: 'Hired', color: '#059669' },
    { key: 'rejected', label: 'Rejected', color: '#dc2626' }
  ];

  content.innerHTML = `
    <div class="page-header" style="display:flex;align-items:center;justify-content:space-between">
      <div><h1>My Candidates</h1><p class="subtitle">${total} total candidates</p></div>
      <div style="position:absolute;left:50%;transform:translateX(-50%);display:flex;gap:4px;background:#f3f4f6;padding:4px;border-radius:8px">
        <button class="btn btn-sm ${candidateViewMode==='table'?'btn-primary':'btn-outline'}" style="min-width:80px;border-radius:6px" onclick="candidateViewMode='table';renderCandidates()">📋 List</button>
        <button class="btn btn-sm ${candidateViewMode==='kanban'?'btn-primary':'btn-outline'}" style="min-width:80px;border-radius:6px" onclick="candidateViewMode='kanban';renderCandidates()">📊 Board</button>
      </div>
      <div></div>
    </div>

    <!-- Search & Filter Bar -->
    <div class="card" style="padding:12px 16px;margin-bottom:16px">
      <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
        <input type="text" id="cand-search" placeholder="Search by name or email..." value="${candidateSearch}"
          style="flex:1;min-width:200px;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px"
          onkeydown="if(event.key==='Enter'){candidateSearch=this.value;candidatePage=1;renderCandidates()}">
        <select id="cand-status-filter" onchange="candidateStatus=this.value;candidatePage=1;renderCandidates()"
          style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
          <option value="" ${!candidateStatus?'selected':''}>All Statuses</option>
          ${columns.map(c=>`<option value="${c.key}" ${candidateStatus===c.key?'selected':''}>${c.label}</option>`).join('')}
        </select>
        <select onchange="candidateSort=this.value;candidatePage=1;renderCandidates()"
          style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
          <option value="created_at" ${candidateSort==='created_at'?'selected':''}>Newest First</option>
          <option value="first_name" ${candidateSort==='first_name'?'selected':''}>Name</option>
          <option value="ai_score" ${candidateSort==='ai_score'?'selected':''}>AI Score</option>
          <option value="interest_rating" ${candidateSort==='interest_rating'?'selected':''}>Interest Rating</option>
          <option value="status" ${candidateSort==='status'?'selected':''}>Status</option>
        </select>
        <button class="btn btn-sm btn-outline" onclick="candidateSortDir=candidateSortDir==='asc'?'desc':'asc';renderCandidates()" title="Toggle sort direction">
          ${candidateSortDir==='asc'?'↑ Asc':'↓ Desc'}
        </button>
        <button class="btn btn-sm btn-outline" onclick="candidateSearch='';candidateStatus='';candidateSort='created_at';candidateSortDir='desc';candidatePage=1;renderCandidates()">Clear</button>
      </div>
      ${allTags.length ? `<div style="margin-top:8px;display:flex;gap:6px;flex-wrap:wrap;align-items:center">
        <span style="font-size:12px;color:#999;font-weight:600">Tags:</span>
        ${allTags.map(t=>`<button class="btn btn-sm btn-outline" style="font-size:11px;padding:2px 8px;border-radius:12px" onclick="filterByTag('${t.tag}')">${t.tag} (${t.count})</button>`).join('')}
      </div>` : ''}
    </div>

    <!-- Bulk Action Bar -->
    <div id="bulk-bar" style="display:none;background:#111;color:#fff;padding:10px 16px;border-radius:8px;margin-bottom:16px;display:flex;align-items:center;gap:12px">
      <span id="bulk-count">0 selected</span>
      <select id="bulk-status-select" style="padding:6px 10px;border-radius:6px;border:1px solid #333;background:#222;color:#fff;font-size:13px">
        <option value="">Change status to...</option>
        ${columns.map(c=>`<option value="${c.key}">${c.label}</option>`).join('')}
      </select>
      <button class="btn btn-sm btn-primary" onclick="bulkStatusUpdate()" style="font-size:12px">Apply Status</button>
      <button class="btn btn-sm" onclick="bulkDeleteCandidates()" style="font-size:12px;background:#dc2626;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer">Delete Selected</button>
      <button class="btn btn-sm btn-outline" onclick="selectedCandidates.clear();updateBulkBar();renderCandidateRows()" style="font-size:12px;color:#fff;border-color:#555">Deselect All</button>
    </div>

    ${candidateViewMode === 'table' ? `
    <div class="card" style="padding:0;overflow:hidden">
      <table>
        <thead><tr>
          <th style="width:40px"><input type="checkbox" id="select-all-cands" onchange="toggleAllCandidates(this.checked)"></th>
          <th>Name</th><th>Interview</th><th>Status</th><th>Score</th><th>Interest</th><th>Tags</th><th>Date</th><th></th>
        </tr></thead>
        <tbody id="cand-table-body">
        ${candidates.map(c => `<tr data-cid="${c.id}">
          <td><input type="checkbox" class="cand-check" data-id="${c.id}" onchange="toggleCandidateSelect('${c.id}',this.checked)"></td>
          <td><a href="/review/${c.id}" style="color:var(--text);text-decoration:none"><strong>${c.first_name} ${c.last_name}</strong><br><span style="font-size:12px;color:#999">${c.email}</span></a></td>
          <td style="font-size:13px">${c.interview_title || '—'}</td>
          <td>${statusBadge(c.status)}</td>
          <td>${c.ai_score ? scoreRing(c.ai_score, 36) : '—'}</td>
          <td>${c.interest_rating ? `<span style="display:inline-flex;align-items:center;gap:4px;background:${c.interest_rating>=8?'rgba(10,206,10,.1)':c.interest_rating>=5?'rgba(255,165,0,.1)':'rgba(220,38,38,.1)'};color:${c.interest_rating>=8?'#0ace0a':c.interest_rating>=5?'#f59e0b':'#dc2626'};padding:3px 10px;border-radius:12px;font-size:13px;font-weight:600">${c.interest_rating}/10</span>` : '<span style="color:#ccc">—</span>'}</td>
          <td id="tags-cell-${c.id}" style="font-size:12px">—</td>
          <td style="font-size:13px">${formatDate(c.created_at)}</td>
          <td><a href="/review/${c.id}" class="btn btn-sm btn-outline">Review</a></td>
        </tr>`).join('')}
        ${candidates.length === 0 ? '<tr><td colspan="9" style="text-align:center;padding:40px;color:#999">No candidates found</td></tr>' : ''}
        </tbody>
      </table>
    </div>
    <!-- Pagination -->
    ${pages_count > 1 ? `<div style="display:flex;justify-content:center;gap:8px;margin-top:16px;align-items:center">
      <button class="btn btn-sm btn-outline" ${candidatePage<=1?'disabled':''} onclick="candidatePage--;renderCandidates()">← Prev</button>
      <span style="font-size:14px;color:#666">Page ${candidatePage} of ${pages_count}</span>
      <button class="btn btn-sm btn-outline" ${candidatePage>=pages_count?'disabled':''} onclick="candidatePage++;renderCandidates()">Next →</button>
    </div>` : ''}
    ` : `
    <div class="kanban">
      ${columns.map(col => {
        const items = candidates.filter(c => c.status === col.key);
        return `<div class="kanban-col">
          <div class="kanban-col-header" style="border-bottom-color:${col.color}">
            ${col.label} <span class="count">${items.length}</span>
          </div>
          <div class="kanban-cards">
            ${items.map(c => `
              <div class="kanban-card" onclick="window.location.href='/review/${c.id}'">
                <div class="name">${c.first_name} ${c.last_name}</div>
                <div class="meta">${c.interview_title}</div>
                <div class="meta">${c.email}</div>
                ${c.ai_score ? `<div class="score" style="color:${scoreColor(c.ai_score)}">Score: ${c.ai_score}</div>` : ''}
              </div>
            `).join('')}
            ${items.length === 0 ? '<div style="text-align:center;padding:20px;color:#999;font-size:13px">No candidates</div>' : ''}
          </div>
        </div>`;
      }).join('')}
    </div>
    `}
  `;

  // Load tags for each candidate in table view
  if (candidateViewMode === 'table') {
    candidates.forEach(async c => {
      try {
        const tags = await api(`/api/candidates/${c.id}/tags`);
        const cell = document.getElementById(`tags-cell-${c.id}`);
        if (cell && tags.length) {
          cell.innerHTML = tags.map(t => `<span style="display:inline-block;padding:2px 8px;background:#e6fce6;color:#059669;border-radius:10px;margin:1px 2px;font-size:11px">${t}</span>`).join('');
        }
      } catch(e) {}
    });
  }

  updateBulkBar();
}

async function filterByTag(tag) {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';
  try {
    const candidates = await api(`/api/candidates/by-tag/${encodeURIComponent(tag)}`);
    content.innerHTML = `
      <div class="page-header">
        <div><h1>Tag: <span style="color:#0ace0a">${tag}</span></h1><p class="subtitle">${candidates.length} candidates</p></div>
        <div class="page-actions"><button class="btn btn-outline" onclick="renderCandidates()">← Back to All</button></div>
      </div>
      <div class="card" style="padding:0;overflow:hidden">
        <table><thead><tr><th>Name</th><th>Interview</th><th>Status</th><th></th></tr></thead><tbody>
        ${candidates.map(c => `<tr>
          <td><strong>${c.first_name} ${c.last_name}</strong><br><span style="font-size:12px;color:#999">${c.email}</span></td>
          <td style="font-size:13px">${c.interview_title || '—'}</td>
          <td>${statusBadge(c.status)}</td>
          <td><a href="/review/${c.id}" class="btn btn-sm btn-outline">Review</a></td>
        </tr>`).join('')}
        ${candidates.length===0?'<tr><td colspan="4" style="text-align:center;padding:40px;color:#999">No candidates with this tag</td></tr>':''}
        </tbody></table>
      </div>
    `;
  } catch(e) { toast(e.message, 'error'); }
}

function toggleCandidateSelect(id, checked) {
  if (checked) selectedCandidates.add(id); else selectedCandidates.delete(id);
  updateBulkBar();
}

function toggleAllCandidates(checked) {
  document.querySelectorAll('.cand-check').forEach(cb => {
    cb.checked = checked;
    const id = cb.dataset.id;
    if (checked) selectedCandidates.add(id); else selectedCandidates.delete(id);
  });
  updateBulkBar();
}

function updateBulkBar() {
  const bar = document.getElementById('bulk-bar');
  if (!bar) return;
  const count = selectedCandidates.size;
  bar.style.display = count > 0 ? 'flex' : 'none';
  const countEl = document.getElementById('bulk-count');
  if (countEl) countEl.textContent = `${count} selected`;
}

async function bulkStatusUpdate() {
  const status = document.getElementById('bulk-status-select').value;
  if (!status) { toast('Select a status first', 'error'); return; }
  if (selectedCandidates.size === 0) return;
  try {
    const res = await api('/api/candidates/bulk-status', {
      method: 'POST', body: JSON.stringify({ candidate_ids: [...selectedCandidates], status })
    });
    toast(`Updated ${res.updated} candidates to ${status}`, 'success');
    selectedCandidates.clear();
    renderCandidates();
  } catch(e) { toast(e.message, 'error'); }
}

async function bulkDeleteCandidates() {
  if (selectedCandidates.size === 0) return;
  if (!confirm(`Delete ${selectedCandidates.size} candidate(s)? This cannot be undone.`)) return;
  try {
    const res = await api('/api/candidates/bulk-delete', {
      method: 'POST', body: JSON.stringify({ candidate_ids: [...selectedCandidates] })
    });
    toast(`Deleted ${res.deleted} candidates`, 'success');
    selectedCandidates.clear();
    renderCandidates();
  } catch(e) { toast(e.message, 'error'); }
}

// ==================== REVIEW PAGE ====================

async function renderReview() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading review...</div>';
  const c = await api(`/api/candidates/${EXTRA_ID}`);
  content.innerHTML = `
    <div class="page-header">
      <div>
        <h1>${c.first_name} ${c.last_name}</h1>
        <p class="subtitle">${c.interview_title} · ${c.email}</p>
      </div>
      <div class="page-actions">
        <button class="btn btn-outline" onclick="openShareModal('${c.id}','${c.first_name} ${c.last_name}','${(c.interview_title||'').replace(/'/g,"\\'")}')">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" style="vertical-align:-2px;margin-right:4px"><circle cx="18" cy="5" r="3"/><circle cx="6" cy="12" r="3"/><circle cx="18" cy="19" r="3"/><line x1="8.59" y1="13.51" x2="15.42" y2="17.49"/><line x1="15.41" y1="6.51" x2="8.59" y2="10.49"/></svg>
          Share Report
        </button>
        ${c.status === 'completed' || c.status === 'reviewed' ? `
          <button class="btn btn-outline" onclick="transcribeCandidate('${c.id}')">📝 Transcribe</button>
          <button class="btn btn-primary" onclick="scoreCandidate('${c.id}')">🤖 Run AI Scoring</button>
        ` : ''}
        <select onchange="updateStatus('${c.id}',this.value)" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
          ${['invited','in_progress','completed','reviewed','hired','rejected'].map(s =>
            `<option value="${s}" ${s===c.status?'selected':''}>${s.replace('_',' ')}</option>`
          ).join('')}
        </select>
      </div>
    </div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:24px">
      <div>
        ${c.responses.length ? c.responses.map((r, i) => `
          <div class="card">
            <div class="card-header">
              <h3>Q${r.question_order}: ${r.question_text}</h3>
              ${r.ai_score ? scoreRing(r.ai_score, 44) : ''}
            </div>
            ${r.video_path ? `
              <div class="video-container" style="margin-bottom:12px">
                <video controls src="${r.video_path}" style="width:100%;border-radius:8px"></video>
              </div>` : '<div style="background:#f3f4f6;border-radius:8px;padding:40px;text-align:center;color:#999;margin-bottom:12px">No video recorded</div>'}
            ${r.ai_feedback ? `<div style="background:#f0fdf4;border:1px solid #d1fae5;border-radius:8px;padding:12px;font-size:14px">
              <strong style="color:#059669">AI Feedback:</strong> ${r.ai_feedback}
              ${renderResponseCategories(r.ai_scores_json)}
            </div>` : ''}
          </div>
        `).join('') : '<div class="card empty-state"><h3>No responses yet</h3><p>This candidate hasn\'t completed their interview.</p></div>'}
      </div>
      <div>
        <div class="card">
          <h3 style="margin-bottom:12px">Candidate Info</h3>
          <div style="font-size:14px;line-height:2">
            <div><strong>Status:</strong> ${statusBadge(c.status)}</div>
            <div><strong>Email:</strong> ${c.email}</div>
            <div><strong>Phone:</strong> ${c.phone || '—'}</div>
            <div><strong>Invited:</strong> ${formatDate(c.invited_at)}</div>
            <div><strong>Started:</strong> ${formatDate(c.started_at)}</div>
            <div><strong>Completed:</strong> ${formatDate(c.completed_at)}</div>
          </div>
        </div>
        ${c.ai_score ? `<div class="card" style="text-align:center">
          <h3 style="margin-bottom:16px">Overall AI Score</h3>
          ${scoreRing(c.ai_score, 80)}
          <p style="margin-top:12px;font-size:14px;color:#666">${c.ai_summary || ''}</p>
          ${renderCategoryBreakdown(c.ai_scores_json)}
        </div>` : ''}
        ${c.interest_rating ? `<div class="card" style="text-align:center">
          <h3 style="margin-bottom:12px">Candidate Interest</h3>
          <div style="font-size:48px;font-weight:800;color:${c.interest_rating>=8?'#0ace0a':c.interest_rating>=5?'#f59e0b':'#dc2626'}">${c.interest_rating}<span style="font-size:20px;color:#999">/10</span></div>
          <p style="margin-top:8px;font-size:14px;color:#888">${c.interest_rating>=8?'Highly interested — ready for a live conversation':c.interest_rating>=5?'Somewhat interested — may need a nudge':'Low interest — probably not the right fit'}</p>
          ${c.interest_comment ? `<p style="margin-top:8px;font-size:13px;color:#666;font-style:italic">"${c.interest_comment}"</p>` : ''}
        </div>` : ''}
        <div class="card">
          <h3 style="margin-bottom:12px">Notes</h3>
          <textarea id="candidate-notes" style="width:100%;min-height:100px;padding:10px;border:1px solid #e5e7eb;border-radius:8px;font-family:inherit;font-size:14px" placeholder="Add notes about this candidate...">${c.notes || ''}</textarea>
          <button class="btn btn-sm btn-primary" style="margin-top:8px" onclick="saveNotes('${c.id}')">Save Notes</button>
        </div>
        <!-- Tags -->
        <div class="card">
          <h3 style="margin-bottom:12px">Tags</h3>
          <div id="review-tags" style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:10px"><span style="color:#999;font-size:13px">Loading...</span></div>
          <div style="display:flex;gap:6px">
            <input type="text" id="new-tag-input" placeholder="Add tag..." style="flex:1;padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px" onkeydown="if(event.key==='Enter')addTagToCandidate('${c.id}')">
            <button class="btn btn-sm btn-primary" onclick="addTagToCandidate('${c.id}')">Add</button>
          </div>
        </div>
        <!-- Custom Fields -->
        <div class="card">
          <h3 style="margin-bottom:12px">Custom Fields</h3>
          <div id="review-custom-fields"><span style="color:#999;font-size:13px">Loading...</span></div>
          <button class="btn btn-sm btn-primary" style="margin-top:10px" onclick="saveCustomFields('${c.id}')">Save Fields</button>
        </div>
      </div>
    </div>
  `;
  loadReviewTags(c.id);
  loadReviewCustomFields(c.id);
}

async function transcribeCandidate(id) {
  toast('Transcribing video responses...', '');
  try {
    const res = await api(`/api/candidates/${id}/transcribe`, { method: 'POST' });
    toast(res.message, 'success');
    if (res.transcribed > 0) renderReview();
  } catch (err) {
    toast(err.message || 'Transcription service not available. Videos will use browser-side captions.', 'error');
  }
}

async function scoreCandidate(id) {
  toast('Running AI analysis... This may take a moment.', '');
  try {
    const res = await api(`/api/candidates/${id}/score`, { method: 'POST' });
    const mode = res.ai_powered ? 'Claude AI' : 'Mock';
    toast(`${mode} Score: ${res.score}/100 — ${res.summary}`, 'success');
    renderReview();
  } catch (err) { toast(err.message, 'error'); }
}

async function updateStatus(id, status) {
  try {
    await api(`/api/candidates/${id}/status`, { method: 'PUT', body: JSON.stringify({ status }) });
    toast('Status updated', 'success');
  } catch (err) { toast(err.message, 'error'); }
}

async function saveNotes(id) {
  try {
    await api(`/api/candidates/${id}/notes`, { method: 'PUT', body: JSON.stringify({ notes: document.getElementById('candidate-notes').value }) });
    toast('Notes saved', 'success');
  } catch (err) { toast(err.message, 'error'); }
}

// ==================== REVIEW: TAGS ====================

async function loadReviewTags(candidateId) {
  const container = document.getElementById('review-tags');
  if (!container) return;
  try {
    const tags = await api(`/api/candidates/${candidateId}/tags`);
    if (tags.length === 0) { container.innerHTML = '<span style="color:#999;font-size:13px">No tags</span>'; return; }
    container.innerHTML = tags.map(t =>
      `<span style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:#e6fce6;color:#059669;border-radius:12px;font-size:12px;font-weight:500">${t}
        <button onclick="removeTagFromCandidate('${candidateId}','${t}')" style="background:none;border:none;color:#059669;cursor:pointer;font-size:14px;padding:0;line-height:1">&times;</button>
      </span>`
    ).join('');
  } catch(e) { container.innerHTML = '<span style="color:#999;font-size:13px">Failed to load tags</span>'; }
}

async function addTagToCandidate(candidateId) {
  const input = document.getElementById('new-tag-input');
  const tag = input.value.trim();
  if (!tag) return;
  try {
    await api(`/api/candidates/${candidateId}/tags`, { method: 'POST', body: JSON.stringify({ tag }) });
    input.value = '';
    toast('Tag added', 'success');
    loadReviewTags(candidateId);
  } catch(e) { toast(e.message, 'error'); }
}

async function removeTagFromCandidate(candidateId, tag) {
  try {
    await api(`/api/candidates/${candidateId}/tags/${encodeURIComponent(tag)}`, { method: 'DELETE' });
    toast('Tag removed', 'success');
    loadReviewTags(candidateId);
  } catch(e) { toast(e.message, 'error'); }
}

// ==================== REVIEW: CUSTOM FIELDS ====================

async function loadReviewCustomFields(candidateId) {
  const container = document.getElementById('review-custom-fields');
  if (!container) return;
  try {
    const [defs, vals] = await Promise.all([
      api('/api/custom-fields'),
      api(`/api/candidates/${candidateId}/custom-fields`)
    ]);
    if (defs.length === 0) { container.innerHTML = '<span style="color:#999;font-size:13px">No custom fields defined. <a href="/settings" style="color:#0ace0a">Add in Settings</a></span>'; return; }
    const valMap = {};
    vals.forEach(v => { valMap[v.field_id] = v.value; });
    container.innerHTML = defs.map(f => {
      const val = valMap[f.id] || '';
      let input = '';
      if (f.field_type === 'boolean') {
        input = `<select class="cf-input" data-fid="${f.id}" style="padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;width:100%">
          <option value="" ${!val?'selected':''}>—</option><option value="true" ${val==='true'?'selected':''}>Yes</option><option value="false" ${val==='false'?'selected':''}>No</option>
        </select>`;
      } else if (f.field_type === 'select' && f.options) {
        const opts = f.options.split(',').map(o=>o.trim());
        input = `<select class="cf-input" data-fid="${f.id}" style="padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;width:100%">
          <option value="">—</option>${opts.map(o=>`<option value="${o}" ${val===o?'selected':''}>${o}</option>`).join('')}
        </select>`;
      } else if (f.field_type === 'date') {
        input = `<input type="date" class="cf-input" data-fid="${f.id}" value="${val}" style="padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;width:100%">`;
      } else if (f.field_type === 'number') {
        input = `<input type="number" class="cf-input" data-fid="${f.id}" value="${val}" style="padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;width:100%">`;
      } else {
        input = `<input type="text" class="cf-input" data-fid="${f.id}" value="${val}" style="padding:6px 10px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px;width:100%">`;
      }
      return `<div style="margin-bottom:10px">
        <label style="font-size:12px;font-weight:600;color:#555;display:block;margin-bottom:3px">${f.field_name} <span style="color:#999;font-weight:400">(${f.field_type})</span></label>
        ${input}
      </div>`;
    }).join('');
  } catch(e) { container.innerHTML = '<span style="color:#999;font-size:13px">Failed to load</span>'; }
}

async function saveCustomFields(candidateId) {
  const inputs = document.querySelectorAll('.cf-input');
  const fields = {};
  inputs.forEach(el => { if (el.value) fields[el.dataset.fid] = el.value; });
  if (Object.keys(fields).length === 0) { toast('No field values to save', ''); return; }
  try {
    await api(`/api/candidates/${candidateId}/custom-fields`, { method: 'PUT', body: JSON.stringify({ fields }) });
    toast('Custom fields saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

// ==================== SHARE REPORT ====================

function openShareModal(candidateId, candidateName, interviewTitle) {
  // Remove any existing modal
  const old = document.getElementById('share-modal');
  if (old) old.remove();

  const defaultTitle = `${candidateName} — ${interviewTitle || 'Interview Assessment'}`;

  const modal = document.createElement('div');
  modal.id = 'share-modal';
  modal.innerHTML = `
    <div style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)closeShareModal()">
      <div style="background:#fff;border-radius:16px;width:520px;max-width:95vw;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,.2)">
        <div style="padding:24px 24px 0">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
            <h2 style="font-size:18px;font-weight:700">Share Candidate Report</h2>
            <button onclick="closeShareModal()" style="background:none;border:none;font-size:24px;cursor:pointer;color:#999;line-height:1">&times;</button>
          </div>
          <p style="font-size:14px;color:#666;margin-bottom:20px">Generate a polished, branded report for <strong>${candidateName}</strong> that you can share with hiring managers.</p>
        </div>

        <div id="share-form" style="padding:0 24px 24px">
          <div style="margin-bottom:16px">
            <label style="display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:4px">Report Title</label>
            <input type="text" id="share-title" value="${defaultTitle}" style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
          </div>

          <div style="margin-bottom:16px">
            <label style="display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:4px">Custom Message (optional)</label>
            <textarea id="share-message" rows="2" placeholder="Add a note for the recipient..." style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px;font-family:inherit;resize:vertical"></textarea>
          </div>

          <div style="margin-bottom:16px">
            <label style="display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:8px">Include in Report</label>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
              <label style="display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;font-size:14px">
                <input type="checkbox" id="share-scores" checked> AI Scores
              </label>
              <label style="display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;font-size:14px">
                <input type="checkbox" id="share-feedback" checked> AI Feedback
              </label>
              <label style="display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;font-size:14px">
                <input type="checkbox" id="share-notes"> Reviewer Notes
              </label>
              <label style="display:flex;align-items:center;gap:8px;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;cursor:pointer;font-size:14px">
                <input type="checkbox" id="share-videos"> Video Links
              </label>
            </div>
          </div>

          <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
            <div>
              <label style="display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:4px">Password (optional)</label>
              <input type="text" id="share-password" placeholder="Leave blank for no password" style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
            </div>
            <div>
              <label style="display:block;font-size:13px;font-weight:600;color:#555;margin-bottom:4px">Expires In</label>
              <select id="share-expires" style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px">
                <option value="">Never</option>
                <option value="7" selected>7 days</option>
                <option value="14">14 days</option>
                <option value="30">30 days</option>
                <option value="90">90 days</option>
              </select>
            </div>
          </div>

          <button class="btn btn-primary" style="width:100%;padding:12px;font-size:15px" onclick="generateReport('${candidateId}')">
            Generate Shareable Link
          </button>
        </div>

        <div id="share-result" style="display:none;padding:0 24px 24px">
          <div style="background:#f0fdf4;border:1px solid #d1fae5;border-radius:12px;padding:20px;text-align:center;margin-bottom:16px">
            <svg viewBox="0 0 24 24" fill="none" stroke="#059669" stroke-width="2" width="40" height="40" style="margin-bottom:8px"><path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/></svg>
            <h3 style="color:#059669;margin-bottom:4px">Report Ready!</h3>
            <p style="font-size:13px;color:#666;margin-bottom:16px">Share this link or email it to hiring managers.</p>
            <div style="display:flex;gap:8px">
              <input type="text" id="share-url" readonly style="flex:1;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:14px;background:#fff">
              <button class="btn btn-primary" onclick="copyShareUrl()" style="white-space:nowrap">Copy Link</button>
            </div>
            <div style="margin-top:12px">
              <a id="share-preview-link" href="#" target="_blank" style="font-size:13px;color:#0ace0a">Open report in new tab &#8599;</a>
            </div>
          </div>

          <!-- Email to managers section -->
          <div style="border:1px solid #e5e7eb;border-radius:12px;padding:20px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <h4 style="font-size:14px;font-weight:600">Email to Managers</h4>
              <button class="btn btn-sm btn-outline" onclick="toggleAddManager()" style="font-size:12px;padding:4px 10px">+ Add New</button>
            </div>

            <!-- Add new manager form (hidden by default) -->
            <div id="add-manager-form" style="display:none;background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:12px">
              <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
                <input type="text" id="new-mgr-name" placeholder="Name" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
                <input type="email" id="new-mgr-email" placeholder="Email" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
              </div>
              <div style="display:flex;gap:8px">
                <input type="text" id="new-mgr-title" placeholder="Title (optional)" style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
                <button class="btn btn-primary btn-sm" onclick="addManager()" style="font-size:12px;padding:6px 14px">Add</button>
              </div>
            </div>

            <!-- Manager list with checkboxes -->
            <div id="manager-list" style="margin-bottom:12px">
              <div style="text-align:center;color:#999;font-size:13px;padding:12px">Loading managers...</div>
            </div>

            <button id="send-report-btn" class="btn btn-primary" style="width:100%;padding:10px;font-size:14px;display:none" onclick="sendReportToManagers()">
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" style="vertical-align:-2px;margin-right:4px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
              Send Report via Email
            </button>
          </div>
        </div>

        <!-- Existing reports -->
        <div id="share-existing" style="padding:0 24px 24px"></div>
      </div>
    </div>
  `;
  document.body.appendChild(modal);

  // Load existing reports
  loadExistingReports(candidateId);
}

function closeShareModal() {
  const modal = document.getElementById('share-modal');
  if (modal) modal.remove();
}

async function generateReport(candidateId) {
  const btn = document.querySelector('#share-form .btn-primary');
  btn.textContent = 'Generating...';
  btn.disabled = true;

  try {
    const res = await api(`/api/candidates/${candidateId}/report`, {
      method: 'POST',
      body: JSON.stringify({
        title: document.getElementById('share-title').value,
        custom_message: document.getElementById('share-message').value,
        include_scores: document.getElementById('share-scores').checked,
        include_ai_feedback: document.getElementById('share-feedback').checked,
        include_notes: document.getElementById('share-notes').checked,
        include_videos: document.getElementById('share-videos').checked,
        password: document.getElementById('share-password').value,
        expires_days: document.getElementById('share-expires').value || null
      })
    });

    _currentReportId = res.report_id;
    document.getElementById('share-form').style.display = 'none';
    document.getElementById('share-result').style.display = 'block';
    document.getElementById('share-url').value = res.url;
    document.getElementById('share-preview-link').href = res.url;

    // Load manager list for email sending
    loadManagerList();

    // Refresh existing reports list
    loadExistingReports(candidateId);
    toast('Report link generated!', 'success');
  } catch (err) {
    toast(err.message, 'error');
    btn.textContent = 'Generate Shareable Link';
    btn.disabled = false;
  }
}

function copyShareUrl() {
  const url = document.getElementById('share-url');
  url.select();
  navigator.clipboard.writeText(url.value);
  toast('Link copied to clipboard!', 'success');
}

async function loadExistingReports(candidateId) {
  try {
    const reports = await api(`/api/candidates/${candidateId}/reports`);
    const container = document.getElementById('share-existing');
    if (!reports.length) { container.innerHTML = ''; return; }

    container.innerHTML = `
      <div style="border-top:1px solid #e5e7eb;padding-top:16px">
        <h4 style="font-size:13px;font-weight:600;color:#999;text-transform:uppercase;letter-spacing:.5px;margin-bottom:12px">Previous Reports</h4>
        ${reports.map(r => `
          <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:8px">
            <div style="font-size:14px">
              <span style="font-weight:500">${r.title || 'Untitled Report'}</span>
              <span style="color:#999;font-size:12px;margin-left:8px">${r.views} view${r.views !== 1 ? 's' : ''}</span>
              ${r.has_password ? '<span style="color:#999;font-size:12px;margin-left:4px">🔒</span>' : ''}
            </div>
            <div style="display:flex;gap:6px">
              <button class="btn btn-sm btn-outline" onclick="navigator.clipboard.writeText(window.location.origin+'/report/${r.token}');toast('Copied!','success')" title="Copy link">Copy</button>
              <button class="btn btn-sm" onclick="deleteReport('${r.id}','${candidateId}')" title="Delete" style="color:#dc2626;border-color:#fecaca">Delete</button>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } catch (err) { /* ignore */ }
}

async function deleteReport(reportId, candidateId) {
  if (!confirm('Delete this shared report link? Anyone who has the link will no longer be able to view it.')) return;
  try {
    await api(`/api/reports/${reportId}`, { method: 'DELETE' });
    toast('Report deleted', 'success');
    loadExistingReports(candidateId);
  } catch (err) { toast(err.message, 'error'); }
}

// ==================== MANAGER CONTACTS ====================

let _currentReportId = null;

function toggleAddManager() {
  const form = document.getElementById('add-manager-form');
  form.style.display = form.style.display === 'none' ? 'block' : 'none';
}

async function loadManagerList() {
  const container = document.getElementById('manager-list');
  if (!container) return;
  try {
    const managers = await api('/api/managers');
    if (!managers.length) {
      container.innerHTML = `<div style="text-align:center;color:#999;font-size:13px;padding:12px">
        No managers yet. Click "+ Add New" to add your first contact.
      </div>`;
      document.getElementById('send-report-btn').style.display = 'none';
      return;
    }
    container.innerHTML = managers.map(m => `
      <label style="display:flex;align-items:center;gap:10px;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px;cursor:pointer;transition:background .15s" onmouseover="this.style.background='#f9fafb'" onmouseout="this.style.background=''">
        <input type="checkbox" class="mgr-check" value="${m.id}" style="width:16px;height:16px" onchange="updateSendBtn()">
        <div style="flex:1;min-width:0">
          <div style="font-size:14px;font-weight:500">${m.name}</div>
          <div style="font-size:12px;color:#999;overflow:hidden;text-overflow:ellipsis">${m.email}${m.title ? ' · ' + m.title : ''}</div>
        </div>
        <button onclick="event.preventDefault();event.stopPropagation();removeManager('${m.id}')" style="background:none;border:none;color:#ccc;cursor:pointer;font-size:16px;padding:2px 4px" title="Remove">&times;</button>
      </label>
    `).join('');
    document.getElementById('send-report-btn').style.display = 'none';
  } catch (err) {
    container.innerHTML = '<div style="color:#dc2626;font-size:13px;padding:8px">Failed to load managers</div>';
  }
}

function updateSendBtn() {
  const checked = document.querySelectorAll('.mgr-check:checked');
  const btn = document.getElementById('send-report-btn');
  if (btn) {
    btn.style.display = checked.length > 0 ? 'block' : 'none';
    btn.innerHTML = `
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16" style="vertical-align:-2px;margin-right:4px"><line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/></svg>
      Send to ${checked.length} Manager${checked.length > 1 ? 's' : ''}
    `;
  }
}

async function addManager() {
  const name = document.getElementById('new-mgr-name').value.trim();
  const email = document.getElementById('new-mgr-email').value.trim();
  const title = document.getElementById('new-mgr-title').value.trim();
  if (!name || !email) { toast('Name and email required', 'error'); return; }

  try {
    await api('/api/managers', {
      method: 'POST',
      body: JSON.stringify({ name, email, title })
    });
    document.getElementById('new-mgr-name').value = '';
    document.getElementById('new-mgr-email').value = '';
    document.getElementById('new-mgr-title').value = '';
    document.getElementById('add-manager-form').style.display = 'none';
    toast('Manager added', 'success');
    loadManagerList();
  } catch (err) { toast(err.message, 'error'); }
}

async function removeManager(id) {
  if (!confirm('Remove this manager from your contacts?')) return;
  try {
    await api(`/api/managers/${id}`, { method: 'DELETE' });
    toast('Manager removed', 'success');
    loadManagerList();
  } catch (err) { toast(err.message, 'error'); }
}

async function sendReportToManagers() {
  if (!_currentReportId) { toast('No report generated yet', 'error'); return; }
  const checked = Array.from(document.querySelectorAll('.mgr-check:checked')).map(c => c.value);
  if (!checked.length) { toast('Select at least one manager', 'error'); return; }

  const btn = document.getElementById('send-report-btn');
  btn.disabled = true;
  btn.textContent = 'Sending...';

  try {
    const res = await api(`/api/reports/${_currentReportId}/send`, {
      method: 'POST',
      body: JSON.stringify({ manager_ids: checked })
    });
    if (res.errors && res.errors.length) {
      toast(`Sent to ${res.sent}/${res.total}. ${res.errors.length} failed — check SMTP settings.`, 'warning');
    } else {
      toast(`Report sent to ${res.sent} manager${res.sent > 1 ? 's' : ''}!`, 'success');
    }
    // Uncheck all
    document.querySelectorAll('.mgr-check').forEach(c => c.checked = false);
    updateSendBtn();
  } catch (err) {
    toast(err.message, 'error');
  }
  btn.disabled = false;
  updateSendBtn();
}

// ==================== SETTINGS ====================

async function renderSettings() {
  // Fetch full profile including SMTP settings
  let profile = APP_USER;
  try { profile = await api('/api/me'); } catch(e) { /* fallback to APP_USER */ }

  content.innerHTML = `
    <div class="page-header"><div><h1>Settings</h1><p class="subtitle">Your agency settings and preferences</p></div></div>
    <div style="max-width:600px">
      <div class="card">
        <h3 style="margin-bottom:16px">Agency Profile</h3>
        <div class="form-group"><label>Your Name</label><input type="text" id="set-name" value="${profile.name || ''}"></div>
        <div class="form-group"><label>Agency Name</label><input type="text" id="set-agency" value="${profile.agency_name || ''}"></div>
        <div class="form-group"><label>Brand Color</label><input type="color" id="set-color" value="${profile.brand_color || '#0ace0a'}" style="height:42px;width:100px"></div>
        <button class="btn btn-primary" onclick="saveSettings()">Save Settings</button>
      </div>

      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div>
            <h3>Email Configuration (SMTP)</h3>
            <p style="font-size:13px;color:#999;margin-top:4px">Required to send report links and candidate emails</p>
          </div>
          ${profile.smtp_configured
            ? '<span style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:#d1fae5;color:#059669;border-radius:6px;font-size:12px;font-weight:600">Connected</span>'
            : '<span style="display:inline-flex;align-items:center;gap:4px;padding:4px 10px;background:#fef3c7;color:#d97706;border-radius:6px;font-size:12px;font-weight:600">Not configured</span>'}
        </div>
        <div style="display:grid;grid-template-columns:2fr 1fr;gap:12px;margin-bottom:12px">
          <div class="form-group" style="margin-bottom:0"><label>SMTP Host</label><input type="text" id="set-smtp-host" value="${profile.smtp_host || ''}" placeholder="e.g. smtp.gmail.com"></div>
          <div class="form-group" style="margin-bottom:0"><label>Port</label><input type="number" id="set-smtp-port" value="${profile.smtp_port || 587}" placeholder="587"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px">
          <div class="form-group" style="margin-bottom:0"><label>SMTP Username</label><input type="text" id="set-smtp-user" value="${profile.smtp_user || ''}" placeholder="your@email.com"></div>
          <div class="form-group" style="margin-bottom:0"><label>SMTP Password</label><input type="password" id="set-smtp-pass" value="" placeholder="${profile.smtp_user ? '••••••••' : 'App password'}"></div>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
          <div class="form-group" style="margin-bottom:0"><label>From Email</label><input type="text" id="set-smtp-from-email" value="${profile.smtp_from_email || ''}" placeholder="noreply@youragency.com"></div>
          <div class="form-group" style="margin-bottom:0"><label>From Name</label><input type="text" id="set-smtp-from-name" value="${profile.smtp_from_name || ''}" placeholder="Channel One Strategies"></div>
        </div>
        <div style="background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:16px;font-size:13px;color:#666">
          <strong>Gmail users:</strong> Use smtp.gmail.com on port 587. You'll need an <a href="https://myaccount.google.com/apppasswords" target="_blank" style="color:#0ace0a">App Password</a> (not your regular Gmail password).
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-primary" onclick="saveSmtpSettings()">Save Email Settings</button>
          <button class="btn btn-outline" onclick="testSmtpSettings()" id="smtp-test-btn">Send Test Email</button>
        </div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:8px">Account</h3>
        <p style="color:#666;font-size:14px;margin-bottom:12px">Email: ${profile.email}</p>
        <p style="color:#666;font-size:14px">Plan: <span class="badge badge-primary" style="text-transform:capitalize">${profile.plan}</span></p>
      </div>
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h3>Hiring Managers</h3>
          <button class="btn btn-sm btn-outline" onclick="document.getElementById('settings-add-mgr').style.display=document.getElementById('settings-add-mgr').style.display==='none'?'block':'none'">+ Add</button>
        </div>
        <p style="font-size:13px;color:#999;margin-bottom:12px">Manage your list of hiring managers to quickly share reports via email.</p>
        <div id="settings-add-mgr" style="display:none;background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:12px">
          <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:8px">
            <input type="text" id="settings-mgr-name" placeholder="Name" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
            <input type="email" id="settings-mgr-email" placeholder="Email" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
          </div>
          <div style="display:flex;gap:8px">
            <input type="text" id="settings-mgr-title" placeholder="Title (optional)" style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
            <button class="btn btn-primary btn-sm" onclick="addManagerFromSettings()" style="font-size:12px;padding:6px 14px">Add</button>
          </div>
        </div>
        <div id="settings-mgr-list"></div>
      </div>

      <!-- Custom Fields Section -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div><h3>Custom Fields</h3><p style="font-size:13px;color:#999;margin-top:4px">Define custom data fields for your candidates</p></div>
          <button class="btn btn-sm btn-outline" onclick="document.getElementById('add-cf-form').style.display=document.getElementById('add-cf-form').style.display==='none'?'block':'none'">+ Add Field</button>
        </div>
        <div id="add-cf-form" style="display:none;background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:12px">
          <div style="display:grid;grid-template-columns:2fr 1fr;gap:8px;margin-bottom:8px">
            <input type="text" id="cf-name" placeholder="Field name" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
            <select id="cf-type" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
              <option value="text">Text</option><option value="number">Number</option><option value="select">Select</option><option value="date">Date</option><option value="boolean">Yes/No</option>
            </select>
          </div>
          <div style="display:flex;gap:8px">
            <input type="text" id="cf-options" placeholder="Options (comma-separated, for select type)" style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
            <button class="btn btn-primary btn-sm" onclick="addCustomField()" style="font-size:12px">Create</button>
          </div>
        </div>
        <div id="cf-list"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>

      <!-- Webhooks Section -->
      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <div><h3>Webhooks</h3><p style="font-size:13px;color:#999;margin-top:4px">Receive HTTP callbacks when events occur</p></div>
          <button class="btn btn-sm btn-outline" onclick="document.getElementById('add-wh-form').style.display=document.getElementById('add-wh-form').style.display==='none'?'block':'none'">+ Add Webhook</button>
        </div>
        <div id="add-wh-form" style="display:none;background:#f9fafb;border-radius:8px;padding:12px;margin-bottom:12px">
          <div class="form-group" style="margin-bottom:8px"><label style="font-size:12px">Callback URL</label><input type="url" id="wh-url" placeholder="https://your-server.com/webhook" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px"></div>
          <div class="form-group" style="margin-bottom:8px"><label style="font-size:12px">Secret (optional, for HMAC signing)</label><input type="text" id="wh-secret" placeholder="your-webhook-secret" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px"></div>
          <div class="form-group" style="margin-bottom:8px"><label style="font-size:12px">Events</label>
            <div id="wh-events-list" style="display:flex;flex-wrap:wrap;gap:6px">Loading...</div>
          </div>
          <button class="btn btn-primary btn-sm" onclick="addWebhook()">Create Webhook</button>
        </div>
        <div id="wh-list"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>

      <!-- Audit Log Section -->
      <div class="card">
        <h3 style="margin-bottom:12px">Recent Activity (Audit Log)</h3>
        <div id="audit-log-list"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>
  `;
  loadSettingsManagers();
  loadCustomFieldsList();
  loadWebhooksList();
  loadWebhookEvents();
  loadAuditLog();
}

async function loadSettingsManagers() {
  const container = document.getElementById('settings-mgr-list');
  if (!container) return;
  try {
    const managers = await api('/api/managers');
    if (!managers.length) {
      container.innerHTML = '<div style="text-align:center;color:#999;font-size:13px;padding:16px">No managers added yet.</div>';
      return;
    }
    container.innerHTML = managers.map(m => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px">
        <div>
          <div style="font-size:14px;font-weight:500">${m.name}</div>
          <div style="font-size:12px;color:#999">${m.email}${m.title ? ' · ' + m.title : ''}</div>
        </div>
        <button onclick="deleteSettingsManager('${m.id}')" style="background:none;border:none;color:#ccc;cursor:pointer;font-size:18px" title="Remove">&times;</button>
      </div>
    `).join('');
  } catch (err) { container.innerHTML = '<div style="color:#dc2626;font-size:13px">Failed to load</div>'; }
}

async function addManagerFromSettings() {
  const name = document.getElementById('settings-mgr-name').value.trim();
  const email = document.getElementById('settings-mgr-email').value.trim();
  const title = document.getElementById('settings-mgr-title').value.trim();
  if (!name || !email) { toast('Name and email required', 'error'); return; }
  try {
    await api('/api/managers', { method: 'POST', body: JSON.stringify({ name, email, title }) });
    document.getElementById('settings-mgr-name').value = '';
    document.getElementById('settings-mgr-email').value = '';
    document.getElementById('settings-mgr-title').value = '';
    document.getElementById('settings-add-mgr').style.display = 'none';
    toast('Manager added', 'success');
    loadSettingsManagers();
  } catch (err) { toast(err.message, 'error'); }
}

async function deleteSettingsManager(id) {
  if (!confirm('Remove this manager?')) return;
  try {
    await api(`/api/managers/${id}`, { method: 'DELETE' });
    toast('Removed', 'success');
    loadSettingsManagers();
  } catch (err) { toast(err.message, 'error'); }
}

async function saveSettings() {
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({
      name: document.getElementById('set-name').value,
      agency_name: document.getElementById('set-agency').value,
      brand_color: document.getElementById('set-color').value
    })});
    toast('Settings saved', 'success');
  } catch (err) { toast(err.message, 'error'); }
}

async function saveSmtpSettings() {
  const data = {
    smtp_host: document.getElementById('set-smtp-host').value.trim(),
    smtp_port: parseInt(document.getElementById('set-smtp-port').value) || 587,
    smtp_user: document.getElementById('set-smtp-user').value.trim(),
    smtp_from_email: document.getElementById('set-smtp-from-email').value.trim(),
    smtp_from_name: document.getElementById('set-smtp-from-name').value.trim(),
  };
  // Only include password if user typed a new one
  const pass = document.getElementById('set-smtp-pass').value;
  if (pass) data.smtp_pass = pass;

  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(data) });
    toast('Email settings saved', 'success');
    renderSettings(); // Refresh to show connected status
  } catch (err) { toast(err.message, 'error'); }
}

async function testSmtpSettings() {
  const btn = document.getElementById('smtp-test-btn');
  btn.disabled = true;
  btn.textContent = 'Sending...';
  try {
    const res = await api('/api/settings/test-email', { method: 'POST' });
    toast(res.message || 'Test email sent!', 'success');
  } catch (err) {
    toast(err.message, 'error');
  }
  btn.disabled = false;
  btn.textContent = 'Send Test Email';
}

// ==================== SETTINGS: CUSTOM FIELDS ====================

async function loadCustomFieldsList() {
  const container = document.getElementById('cf-list');
  if (!container) return;
  try {
    const fields = await api('/api/custom-fields');
    if (!fields.length) { container.innerHTML = '<div style="text-align:center;color:#999;font-size:13px;padding:16px">No custom fields defined yet.</div>'; return; }
    container.innerHTML = fields.map(f => `
      <div style="display:flex;align-items:center;justify-content:space-between;padding:10px 12px;border:1px solid #e5e7eb;border-radius:8px;margin-bottom:6px">
        <div>
          <div style="font-size:14px;font-weight:500">${f.field_name}</div>
          <div style="font-size:12px;color:#999">${f.field_type}${f.options ? ' · Options: ' + f.options : ''}${f.required ? ' · Required' : ''}</div>
        </div>
        <button onclick="deleteCustomField('${f.id}')" style="background:none;border:none;color:#ccc;cursor:pointer;font-size:18px" title="Delete">&times;</button>
      </div>
    `).join('');
  } catch(e) { container.innerHTML = '<div style="color:#dc2626;font-size:13px">Failed to load</div>'; }
}

async function addCustomField() {
  const name = document.getElementById('cf-name').value.trim();
  const type = document.getElementById('cf-type').value;
  const options = document.getElementById('cf-options').value.trim();
  if (!name) { toast('Field name is required', 'error'); return; }
  try {
    await api('/api/custom-fields', { method: 'POST', body: JSON.stringify({ field_name: name, field_type: type, options }) });
    document.getElementById('cf-name').value = '';
    document.getElementById('cf-options').value = '';
    document.getElementById('add-cf-form').style.display = 'none';
    toast('Custom field created', 'success');
    loadCustomFieldsList();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteCustomField(id) {
  if (!confirm('Delete this custom field? All candidate values for this field will be removed.')) return;
  try {
    await api(`/api/custom-fields/${id}`, { method: 'DELETE' });
    toast('Field deleted', 'success');
    loadCustomFieldsList();
  } catch(e) { toast(e.message, 'error'); }
}

// ==================== SETTINGS: WEBHOOKS ====================

async function loadWebhookEvents() {
  const container = document.getElementById('wh-events-list');
  if (!container) return;
  try {
    const data = await api('/api/webhooks/events');
    container.innerHTML = data.events.map(e =>
      `<label style="display:inline-flex;align-items:center;gap:4px;padding:4px 8px;background:#f3f4f6;border-radius:6px;font-size:12px;cursor:pointer">
        <input type="checkbox" class="wh-event-cb" value="${e.name}"> ${e.name}
      </label>`
    ).join('');
  } catch(e) { container.innerHTML = '<span style="color:#999">Failed to load events</span>'; }
}

async function loadWebhooksList() {
  const container = document.getElementById('wh-list');
  if (!container) return;
  try {
    const hooks = await api('/api/webhooks');
    if (!hooks.length) { container.innerHTML = '<div style="text-align:center;color:#999;font-size:13px;padding:16px">No webhooks configured.</div>'; return; }
    container.innerHTML = hooks.map(h => `
      <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:8px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px">
          <div style="font-size:14px;font-weight:500;word-break:break-all">${h.url}</div>
          <div style="display:flex;gap:6px;align-items:center;flex-shrink:0">
            <span style="font-size:11px;padding:2px 8px;border-radius:10px;${h.active?'background:#d1fae5;color:#059669':'background:#fee2e2;color:#dc2626'}">${h.active?'Active':'Paused'}</span>
            <button onclick="toggleWebhook('${h.id}',${!h.active})" class="btn btn-sm btn-outline" style="font-size:11px;padding:2px 8px">${h.active?'Pause':'Activate'}</button>
            <button onclick="testWebhook('${h.id}')" class="btn btn-sm btn-outline" style="font-size:11px;padding:2px 8px">Test</button>
            <button onclick="deleteWebhook('${h.id}')" style="background:none;border:none;color:#ccc;cursor:pointer;font-size:18px">&times;</button>
          </div>
        </div>
        <div style="font-size:12px;color:#999">Events: ${(h.events||[]).join(', ')}${h.failure_count ? ` · Failures: ${h.failure_count}` : ''}</div>
      </div>
    `).join('');
  } catch(e) { container.innerHTML = '<div style="color:#dc2626;font-size:13px">Failed to load</div>'; }
}

async function addWebhook() {
  const url = document.getElementById('wh-url').value.trim();
  const secret = document.getElementById('wh-secret').value.trim();
  const events = [...document.querySelectorAll('.wh-event-cb:checked')].map(cb => cb.value);
  if (!url) { toast('URL is required', 'error'); return; }
  if (!events.length) { toast('Select at least one event', 'error'); return; }
  try {
    await api('/api/webhooks', { method: 'POST', body: JSON.stringify({ url, events, secret }) });
    document.getElementById('wh-url').value = '';
    document.getElementById('wh-secret').value = '';
    document.querySelectorAll('.wh-event-cb').forEach(cb => cb.checked = false);
    document.getElementById('add-wh-form').style.display = 'none';
    toast('Webhook created', 'success');
    loadWebhooksList();
  } catch(e) { toast(e.message, 'error'); }
}

async function toggleWebhook(id, active) {
  try {
    await api(`/api/webhooks/${id}`, { method: 'PUT', body: JSON.stringify({ active }) });
    toast(active ? 'Webhook activated' : 'Webhook paused', 'success');
    loadWebhooksList();
  } catch(e) { toast(e.message, 'error'); }
}

async function testWebhook(id) {
  try {
    const res = await api(`/api/webhooks/${id}/test`, { method: 'POST' });
    toast(res.success ? 'Webhook test sent!' : 'Test failed: ' + res.error, res.success ? 'success' : 'error');
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteWebhook(id) {
  if (!confirm('Delete this webhook?')) return;
  try {
    await api(`/api/webhooks/${id}`, { method: 'DELETE' });
    toast('Webhook deleted', 'success');
    loadWebhooksList();
  } catch(e) { toast(e.message, 'error'); }
}

// ==================== SETTINGS: AUDIT LOG ====================

async function loadAuditLog() {
  const container = document.getElementById('audit-log-list');
  if (!container) return;
  try {
    const data = await api('/api/audit-log');
    const entries = data.entries || data.logs || (Array.isArray(data) ? data : []);
    if (!entries.length) { container.innerHTML = '<div style="text-align:center;color:#999;font-size:13px;padding:16px">No activity logged yet.</div>'; return; }
    container.innerHTML = entries.slice(0, 20).map(e => `
      <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f3f4f6">
        <div style="flex:1">
          <div style="font-size:13px;font-weight:500">${e.action.replace(/_/g, ' ')}</div>
          <div style="font-size:12px;color:#999">${e.entity_type || ''} · ${formatDate(e.created_at)}</div>
        </div>
      </div>
    `).join('');
  } catch(e) { container.innerHTML = '<div style="color:#dc2626;font-size:13px">Failed to load</div>'; }
}

// ==================== NOTIFICATIONS ====================

async function loadNotifications() {
  try {
    const data = await api('/api/notifications');
    const badge = document.getElementById('notif-badge');
    if (badge) {
      if (data.unread_count > 0) {
        badge.textContent = data.unread_count > 9 ? '9+' : data.unread_count;
        badge.style.display = 'flex';
      } else {
        badge.style.display = 'none';
      }
    }
    return data;
  } catch(e) { return { notifications: [], unread_count: 0 }; }
}

async function toggleNotifPanel() {
  const panel = document.getElementById('notif-panel');
  const overlay = document.getElementById('notif-overlay');
  if (panel.style.display === 'none') {
    panel.style.display = 'block';
    overlay.style.display = 'block';
    await renderNotifList();
  } else {
    panel.style.display = 'none';
    overlay.style.display = 'none';
  }
}

async function renderNotifList() {
  const container = document.getElementById('notif-list');
  if (!container) return;
  try {
    const data = await api('/api/notifications?limit=30');
    if (!data.notifications.length) {
      container.innerHTML = '<div style="text-align:center;color:#999;padding:40px;font-size:14px">No notifications</div>';
      return;
    }
    container.innerHTML = data.notifications.map(n => `
      <div style="padding:12px;border-bottom:1px solid #f3f4f6;${n.is_read?'opacity:.6':'background:#f0fdf4'};cursor:pointer" onclick="deleteNotif('${n.id}')">
        <div style="display:flex;justify-content:space-between;align-items:start">
          <div style="font-size:14px;font-weight:${n.is_read?'400':'600'}">${n.title}</div>
          ${!n.is_read?'<span style="width:8px;height:8px;background:#0ace0a;border-radius:50%;flex-shrink:0;margin-top:6px"></span>':''}
        </div>
        ${n.message?`<div style="font-size:13px;color:#666;margin-top:4px">${n.message}</div>`:''}
        <div style="font-size:11px;color:#999;margin-top:4px">${formatDate(n.created_at)}</div>
      </div>
    `).join('');
  } catch(e) { container.innerHTML = '<div style="color:#dc2626;padding:20px">Failed to load</div>'; }
}

async function markAllNotifRead() {
  try {
    await api('/api/notifications/mark-read', { method: 'POST', body: JSON.stringify({ all: true }) });
    toast('All marked as read', 'success');
    loadNotifications();
    renderNotifList();
  } catch(e) { toast(e.message, 'error'); }
}

async function clearReadNotifs() {
  try {
    await api('/api/notifications/clear', { method: 'POST' });
    toast('Read notifications cleared', 'success');
    loadNotifications();
    renderNotifList();
  } catch(e) { toast(e.message, 'error'); }
}

async function deleteNotif(id) {
  try {
    await api(`/api/notifications/${id}`, { method: 'DELETE' });
    loadNotifications();
    renderNotifList();
  } catch(e) {}
}

// ==================== ANALYTICS & AUDIT LOG PAGE ====================

async function renderAnalytics() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading analytics...</div>';
  try {
    const data = await api('/api/analytics');
    const logs = await api('/api/audit-log').catch(() => []);
    const entries = logs.entries || logs.logs || (Array.isArray(logs) ? logs : []);

    content.innerHTML = `
      <div class="page-header">
        <div><h1>Numbers &amp; Trends</h1><p class="subtitle">How your recruiting is going</p></div>
        <div class="page-actions">
          <a href="/api/analytics/export" class="btn btn-outline" download>📥 Export CSV</a>
        </div>
      </div>

      <div class="stat-grid" style="grid-template-columns:repeat(4,1fr)">
        <div class="stat-card"><div class="stat-label">Total Interviews</div><div class="stat-value">${data.total_interviews || 0}</div></div>
        <div class="stat-card"><div class="stat-label">Total Candidates</div><div class="stat-value">${data.total_candidates || 0}</div></div>
        <div class="stat-card"><div class="stat-label">Completion Rate</div><div class="stat-value">${data.completion_rate ? Math.round(data.completion_rate) + '%' : '—'}</div></div>
        <div class="stat-card"><div class="stat-label">Avg Score</div><div class="stat-value">${data.avg_score ? Math.round(data.avg_score) : '—'}</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <h3 style="margin-bottom:16px">Status Breakdown</h3>
          ${data.status_breakdown ? `<div style="display:flex;flex-direction:column;gap:10px">
            ${Object.entries(data.status_breakdown).map(([status, count]) => `
              <div style="display:flex;justify-content:space-between;align-items:center">
                <span>${statusBadge(status)}</span><span style="font-weight:600">${count}</span>
              </div>
            `).join('')}
          </div>` : '<div class="empty-state"><p>No data yet</p></div>'}
        </div>

        <div class="card">
          <h3 style="margin-bottom:16px">Audit Log</h3>
          <div style="max-height:400px;overflow-y:auto">
            ${entries.length ? entries.slice(0, 30).map(e => `
              <div style="padding:8px 0;border-bottom:1px solid #f3f4f6">
                <div style="font-size:13px;font-weight:500">${e.action.replace(/_/g, ' ')}</div>
                <div style="font-size:12px;color:#999">${e.entity_type || ''} · ${formatDate(e.created_at)}${e.details ? ' · ' + (typeof e.details === 'string' ? e.details.substring(0, 60) : '') : ''}</div>
              </div>
            `).join('') : '<div style="text-align:center;color:#999;padding:20px;font-size:13px">No activity yet</div>'}
          </div>
        </div>
      </div>

      ${data.interview_analytics ? `
      <div class="card" style="margin-top:16px">
        <h3 style="margin-bottom:16px">By Interview</h3>
        <table><thead><tr><th>Interview</th><th>Candidates</th><th>Completed</th><th>Avg Score</th><th>Completion Rate</th></tr></thead><tbody>
        ${data.interview_analytics.map(iv => `<tr>
          <td><strong>${iv.title}</strong></td>
          <td>${iv.total_candidates || 0}</td>
          <td>${iv.completed || 0}</td>
          <td>${iv.avg_score ? Math.round(iv.avg_score) : '—'}</td>
          <td>${iv.total_candidates ? Math.round((iv.completed || 0) / iv.total_candidates * 100) + '%' : '—'}</td>
        </tr>`).join('')}
        </tbody></table>
      </div>` : ''}
    `;
  } catch(e) {
    content.innerHTML = `<div class="empty-state"><h3>Numbers &amp; Trends</h3><p>No data yet. Once you have interviews and candidates, your numbers will show up here.</p></div>`;
  }
}

// ==================== BILLING ====================

// Old billing removed — see Cycle 30 renderBilling below

// ==================== AI INSIGHTS ====================

async function renderAiInsights() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading AI insights...</div>';
  try {
    const data = await api('/api/ai/insights');
    const d = data.score_distribution || {};
    const total = d.total || 0;
    const avg = d.avg_score ? Math.round(d.avg_score * 10) / 10 : '—';

    content.innerHTML = `
      <div class="page-header">
        <div><h1>AI Scoring</h1><p class="subtitle">See how AI rated your candidates</p></div>
      </div>

      <!-- Summary Stats -->
      <div class="stat-grid" style="grid-template-columns:repeat(5,1fr)">
        <div class="stat-card"><div class="stat-label">Total Scored</div><div class="stat-value">${total}</div></div>
        <div class="stat-card"><div class="stat-label">Avg Score</div><div class="stat-value">${avg}</div></div>
        <div class="stat-card"><div class="stat-label">Excellent (90+)</div><div class="stat-value" style="color:#059669">${d.excellent || 0}</div></div>
        <div class="stat-card"><div class="stat-label">Strong (80-89)</div><div class="stat-value" style="color:var(--primary)">${d.strong || 0}</div></div>
        <div class="stat-card"><div class="stat-label">Needs Work (&lt;60)</div><div class="stat-value" style="color:#dc2626">${d.needs_work || 0}</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <!-- Score Distribution Bar -->
        <div class="card">
          <h3 style="margin-bottom:16px">Score Distribution</h3>
          ${total > 0 ? `
          <div style="display:flex;flex-direction:column;gap:12px">
            ${[
              { label: 'Excellent (90-100)', count: d.excellent || 0, color: '#059669' },
              { label: 'Strong (80-89)', count: d.strong || 0, color: '#0ace0a' },
              { label: 'Good (70-79)', count: d.good || 0, color: '#2563eb' },
              { label: 'Fair (60-69)', count: d.fair || 0, color: '#d97706' },
              { label: 'Needs Work (<60)', count: d.needs_work || 0, color: '#dc2626' }
            ].map(b => `
              <div>
                <div style="display:flex;justify-content:space-between;font-size:13px;margin-bottom:4px">
                  <span>${b.label}</span><span style="font-weight:600">${b.count}</span>
                </div>
                <div style="background:#e5e7eb;border-radius:4px;height:24px;overflow:hidden">
                  <div style="background:${b.color};width:${total ? (b.count/total*100) : 0}%;height:100%;border-radius:4px;transition:width .3s"></div>
                </div>
              </div>
            `).join('')}
          </div>` : '<div class="empty-state"><p>No scored candidates yet. Once interviews are completed, AI will score them automatically.</p></div>'}
        </div>

        <!-- Per-Interview Averages -->
        <div class="card">
          <h3 style="margin-bottom:16px">Scores by Interview</h3>
          ${data.interview_scores.length ? `<table><thead><tr><th>Interview</th><th>Scored</th><th>Avg</th><th>Range</th></tr></thead><tbody>
            ${data.interview_scores.map(iv => `<tr>
              <td><strong>${iv.title}</strong><br><span style="font-size:12px;color:#999">${iv.position || ''}</span></td>
              <td>${iv.scored_count}</td>
              <td>${scoreRing(Math.round(iv.avg_score), 40)}</td>
              <td style="font-size:13px;color:#666">${Math.round(iv.min_score)} – ${Math.round(iv.max_score)}</td>
            </tr>`).join('')}
          </tbody></table>` : '<div class="empty-state"><p>No interview scoring data yet.</p></div>'}
        </div>
      </div>

      <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
        <!-- Top Candidates Leaderboard -->
        <div class="card">
          <div class="card-header"><h3>Top Candidates</h3></div>
          ${data.top_candidates.length ? `<table><thead><tr><th>#</th><th>Candidate</th><th>Interview</th><th>Score</th><th>Status</th><th></th></tr></thead><tbody>
            ${data.top_candidates.slice(0, 10).map((c, i) => `<tr>
              <td style="font-weight:700;color:${i < 3 ? 'var(--primary)' : '#999'}">${i + 1}</td>
              <td><strong>${c.first_name} ${c.last_name}</strong><br><span style="font-size:12px;color:#999">${c.email}</span></td>
              <td style="font-size:13px">${c.interview_title}</td>
              <td>${scoreRing(c.ai_score, 44)}</td>
              <td>${statusBadge(c.status)}</td>
              <td><a href="/review/${c.id}" class="btn btn-sm btn-outline">Review</a></td>
            </tr>`).join('')}
          </tbody></table>` : '<div class="empty-state"><p>No scored candidates yet.</p></div>'}
        </div>

        <!-- Question Performance + Recent -->
        <div>
          <div class="card">
            <h3 style="margin-bottom:12px">Question Performance</h3>
            ${data.question_scores.length ? `<div style="display:flex;flex-direction:column;gap:10px">
              ${data.question_scores.slice(0, 8).map(q => `
                <div style="padding:8px 0;border-bottom:1px solid #f3f4f6">
                  <div style="font-size:13px;color:#555;margin-bottom:4px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${q.question_text}">${q.question_text}</div>
                  <div style="display:flex;align-items:center;gap:8px">
                    <div style="flex:1;background:#e5e7eb;border-radius:4px;height:8px">
                      <div style="background:${scoreColor(q.avg_score)};width:${q.avg_score}%;height:100%;border-radius:4px"></div>
                    </div>
                    <span style="font-size:13px;font-weight:600;color:${scoreColor(q.avg_score)}">${Math.round(q.avg_score)}</span>
                  </div>
                </div>
              `).join('')}
            </div>` : '<div style="text-align:center;padding:20px;color:#999;font-size:13px">No question-level data yet</div>'}
          </div>

          <div class="card">
            <h3 style="margin-bottom:12px">Recent Scorings</h3>
            ${data.recent_scorings.length ? data.recent_scorings.map(r => `
              <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid #f3f4f6">
                <div style="flex:1">
                  <div style="font-size:14px;font-weight:600">${r.first_name} ${r.last_name}</div>
                  <div style="font-size:12px;color:#999">${r.interview_title}</div>
                </div>
                ${scoreRing(r.ai_score, 36)}
              </div>
            `).join('') : '<div style="text-align:center;padding:20px;color:#999;font-size:13px">No recent scorings</div>'}
          </div>
        </div>
      </div>
    `;
  } catch (err) {
    content.innerHTML = `<div class="empty-state"><h3>AI Scoring</h3><p>No scoring data yet. Once candidates complete interviews, AI will score them and results will show here.</p></div>`;
  }
}

// ==================== ONBOARDING WIZARD ====================

async function renderOnboarding() {
  const status = await api('/api/onboarding/wizard-status');
  let step = status.current_step || 0;

  function renderStep() {
    const steps = ['Welcome', 'Agency Profile', 'Branding', 'First Interview', 'Complete'];
    const progressPct = (step / 4) * 100;

    let stepContent = '';
    if (step === 0) {
      stepContent = `
        <div style="text-align:center;padding:40px 0">
          <h2 style="font-size:28px;margin-bottom:8px">Welcome to ChannelView</h2>
          <p style="color:#666;font-size:16px;margin-bottom:32px">Let's set up your account in just a few steps.</p>
          <div style="display:flex;flex-direction:column;gap:12px;max-width:400px;margin:0 auto">
            <button class="btn btn-primary btn-lg" onclick="advanceOnboarding(1)">Get Started</button>
            <button class="btn btn-outline" onclick="skipOnboarding()">Skip Setup — I'll do it later</button>
          </div>
        </div>`;
    } else if (step === 1) {
      stepContent = `
        <h2 style="margin-bottom:24px">Agency Profile</h2>
        <div style="max-width:500px">
          <div class="form-group"><label>Agency Name</label><input id="ob-agency" class="form-control" value="${status.agency_name || ''}" placeholder="Your Insurance Agency"></div>
          <div class="form-group"><label>Website (optional)</label><input id="ob-website" class="form-control" placeholder="https://youragency.com"></div>
          <div class="form-group"><label>Phone (optional)</label><input id="ob-phone" class="form-control" placeholder="(555) 123-4567"></div>
          <div style="display:flex;gap:12px;margin-top:24px">
            <button class="btn btn-primary" onclick="saveOnboardingStep(1)">Continue</button>
            <button class="btn btn-outline" onclick="advanceOnboarding(2)">Skip</button>
          </div>
        </div>`;
    } else if (step === 2) {
      stepContent = `
        <h2 style="margin-bottom:24px">Brand Colors</h2>
        <div style="max-width:500px">
          <div class="form-group"><label>Primary Color</label>
            <div style="display:flex;align-items:center;gap:12px">
              <input type="color" id="ob-color" value="${status.brand_color || '#0ace0a'}" style="width:60px;height:40px;border:none;cursor:pointer">
              <span id="ob-color-hex" style="font-family:monospace;font-size:14px">${status.brand_color || '#0ace0a'}</span>
            </div>
          </div>
          <div class="card" style="padding:16px;margin:20px 0">
            <p style="font-size:13px;color:#666;margin-bottom:12px">Preview:</p>
            <div id="ob-preview" style="background:${status.brand_color || '#0ace0a'};color:#000;padding:12px 24px;border-radius:6px;font-weight:700;display:inline-block">Start Interview</div>
          </div>
          <div style="display:flex;gap:12px;margin-top:24px">
            <button class="btn btn-outline" onclick="advanceOnboarding(1)">Back</button>
            <button class="btn btn-primary" onclick="saveOnboardingStep(2)">Continue</button>
            <button class="btn btn-outline" onclick="advanceOnboarding(3)">Skip</button>
          </div>
        </div>`;
      setTimeout(() => {
        const ci = document.getElementById('ob-color');
        if (ci) ci.addEventListener('input', e => {
          document.getElementById('ob-color-hex').textContent = e.target.value;
          document.getElementById('ob-preview').style.background = e.target.value;
        });
      }, 50);
    } else if (step === 3) {
      stepContent = `
        <h2 style="margin-bottom:24px">Create Your First Interview</h2>
        <div style="max-width:500px">
          <div class="form-group"><label>Interview Title</label><input id="ob-title" class="form-control" placeholder="e.g., Insurance Agent Interview"></div>
          <div class="form-group"><label>Department (optional)</label><input id="ob-dept" class="form-control" placeholder="e.g., Sales"></div>
          <div class="form-group"><label>Questions (one per line)</label>
            <textarea id="ob-questions" class="form-control" rows="5" placeholder="Tell me about your experience in insurance.\nHow do you handle client objections?\nWhy are you interested in this role?"></textarea>
          </div>
          <div style="display:flex;gap:12px;margin-top:24px">
            <button class="btn btn-outline" onclick="advanceOnboarding(2)">Back</button>
            <button class="btn btn-primary" onclick="saveOnboardingStep(3)">Create & Finish</button>
            <button class="btn btn-outline" onclick="advanceOnboarding(4)">Skip</button>
          </div>
        </div>`;
    } else {
      stepContent = `
        <div style="text-align:center;padding:40px 0">
          <div style="font-size:48px;margin-bottom:16px">&#x1f389;</div>
          <h2 style="font-size:28px;margin-bottom:8px">You're All Set!</h2>
          <p style="color:#666;font-size:16px;margin-bottom:32px">Your ChannelView account is ready to go.</p>
          <button class="btn btn-primary btn-lg" onclick="window.location.href='/dashboard'">Go to Dashboard</button>
        </div>`;
    }

    content.innerHTML = `
      <div style="max-width:700px;margin:0 auto;padding:40px 20px">
        <div style="display:flex;justify-content:space-between;margin-bottom:8px;font-size:13px;color:#666">
          ${steps.map((s, i) => `<span style="font-weight:${i <= step ? '600' : '400'};color:${i <= step ? 'var(--primary)' : '#999'}">${s}</span>`).join('')}
        </div>
        <div style="background:#e5e7eb;border-radius:4px;height:6px;margin-bottom:40px">
          <div style="background:var(--primary);width:${progressPct}%;height:100%;border-radius:4px;transition:width .3s"></div>
        </div>
        ${stepContent}
      </div>`;
  }

  window.advanceOnboarding = function(s) { step = s; renderStep(); };
  window.skipOnboarding = async function() {
    await api('/api/onboarding/wizard-skip', { method: 'POST' });
    window.location.href = '/dashboard';
  };
  window.saveOnboardingStep = async function(s) {
    let data = {};
    if (s === 1) {
      data = { agency_name: document.getElementById('ob-agency')?.value, agency_website: document.getElementById('ob-website')?.value, agency_phone: document.getElementById('ob-phone')?.value };
    } else if (s === 2) {
      data = { brand_color: document.getElementById('ob-color')?.value };
    } else if (s === 3) {
      const qText = document.getElementById('ob-questions')?.value || '';
      data = { interview_title: document.getElementById('ob-title')?.value, department: document.getElementById('ob-dept')?.value, questions: qText.split('\n').map(q => q.trim()).filter(Boolean) };
    }
    await api('/api/onboarding/wizard-step', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step: s, data }) });
    step = s === 3 ? 4 : s + 1;
    if (step === 4) await api('/api/onboarding/wizard-step', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ step: 4, data: {} }) });
    renderStep();
  };

  renderStep();
}


// ==================== FMO ADMIN PANEL ====================

async function renderAdmin() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading admin panel...</div>';
  try {
    const [stats, accounts] = await Promise.all([api('/api/admin/stats'), api('/api/admin/accounts')]);
    const accts = accounts.accounts || [];

    content.innerHTML = `
      <div class="page-header">
        <div><h1>FMO Admin Panel</h1><p class="subtitle">Platform-wide overview of all agency accounts</p></div>
      </div>

      <div class="stat-grid">
        <div class="stat-card"><div class="stat-label">Total Agencies</div><div class="stat-value">${stats.total_accounts}</div></div>
        <div class="stat-card"><div class="stat-label">Active Subscriptions</div><div class="stat-value" style="color:var(--primary)">${stats.active_subscriptions}</div></div>
        <div class="stat-card"><div class="stat-label">Total Interviews</div><div class="stat-value">${stats.total_interviews}</div></div>
        <div class="stat-card"><div class="stat-label">Total Candidates</div><div class="stat-value">${stats.total_candidates}</div></div>
        <div class="stat-card"><div class="stat-label">Completed</div><div class="stat-value">${stats.completed_interviews}</div></div>
        <div class="stat-card"><div class="stat-label">New Agencies (30d)</div><div class="stat-value">${stats.accounts_this_month}</div></div>
      </div>

      <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
        <div class="card">
          <div class="card-header"><h3>All Agency Accounts</h3></div>
          <table>
            <thead><tr><th>Agency</th><th>Owner</th><th>Plan</th><th>Interviews</th><th>Candidates</th><th>Team</th><th>Joined</th></tr></thead>
            <tbody>
              ${accts.map(a => `<tr>
                <td><strong>${a.agency_name || 'Unnamed'}</strong>${!a.onboarding_completed ? ' <span style="background:#fef3c7;color:#92400e;padding:2px 6px;border-radius:4px;font-size:11px">Setup pending</span>' : ''}</td>
                <td>${a.name}<br><span style="font-size:12px;color:#999">${a.email}</span></td>
                <td>${a.subscription_status === 'active' ? '<span style="color:var(--primary);font-weight:600">Pro</span>' : '<span style="color:#999">Free</span>'}</td>
                <td>${a.interview_count}</td>
                <td>${a.candidate_count} <span style="font-size:12px;color:#999">(${a.completed_count} done)</span></td>
                <td>${a.team_size}</td>
                <td style="font-size:13px;color:#666">${new Date(a.created_at).toLocaleDateString()}</td>
              </tr>`).join('')}
            </tbody>
          </table>
        </div>

        <div class="card">
          <div class="card-header"><h3>Recent Activity</h3></div>
          ${(stats.recent_activity || []).length ? stats.recent_activity.slice(0, 15).map(a => `
            <div style="padding:8px 0;border-bottom:1px solid #f3f4f6;font-size:13px">
              <div><strong>${a.candidate_name}</strong> — ${statusBadge(a.status)}</div>
              <div style="color:#999">${a.agency_name || ''} &middot; ${a.interview_title}</div>
            </div>
          `).join('') : '<div class="empty-state"><p>No recent activity</p></div>'}
        </div>
      </div>`;
  } catch (err) {
    content.innerHTML = `<div class="empty-state"><h3>Access Denied</h3><p>${err.message || 'FMO admin access required.'}</p></div>`;
  }
}


// ==================== WHITE-LABEL BRANDING ====================

async function renderBranding() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading branding settings...</div>';
  try {
    const b = await api('/api/branding');

    content.innerHTML = `
      <div class="page-header">
        <div><h1>My Branding</h1><p class="subtitle">Make it look like yours — logo, colors, and style</p></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <h3 style="margin-bottom:16px">Brand Colors</h3>
          <div class="form-group"><label>Primary Color</label>
            <div style="display:flex;align-items:center;gap:12px">
              <input type="color" id="br-color" value="${b.brand_color}" style="width:60px;height:40px;border:none;cursor:pointer">
              <span id="br-color-hex" style="font-family:monospace">${b.brand_color}</span>
            </div>
          </div>
          <div class="form-group"><label>Secondary Color</label>
            <div style="display:flex;align-items:center;gap:12px">
              <input type="color" id="br-secondary" value="${b.brand_secondary_color}" style="width:60px;height:40px;border:none;cursor:pointer">
              <span id="br-sec-hex" style="font-family:monospace">${b.brand_secondary_color}</span>
            </div>
          </div>
        </div>

        <div class="card">
          <h3 style="margin-bottom:16px">Custom Branding Settings</h3>
          <div class="form-group">
            <label><input type="checkbox" id="br-wl-enabled" ${b.white_label_enabled ? 'checked' : ''}> Use My Own Branding</label>
            <p style="font-size:13px;color:#666;margin-top:4px">When enabled, candidates see your brand name and logo instead of ChannelView.</p>
          </div>
          <div class="form-group"><label>Candidate-Facing Brand Name</label>
            <input id="br-brand-name" class="form-control" value="${b.candidate_brand_name || ''}" placeholder="Your Agency Name">
          </div>
          <div class="form-group"><label>Agency Logo</label>
            <div style="display:flex;align-items:center;gap:12px">
              ${b.agency_logo_url ? `<img src="${b.agency_logo_url}" alt="Logo" style="height:40px;max-width:200px;object-fit:contain">` : '<span style="color:#999;font-size:13px">No logo uploaded</span>'}
              <input type="file" id="br-logo-file" accept=".png,.jpg,.jpeg,.svg,.webp" style="font-size:13px">
            </div>
          </div>
        </div>
      </div>

      <div class="card" style="margin-top:16px">
        <h3 style="margin-bottom:16px">Agency Info</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px">
          <div class="form-group"><label>Agency Name</label><input id="br-name" class="form-control" value="${b.agency_name || ''}"></div>
          <div class="form-group"><label>Website</label><input id="br-website" class="form-control" value="${b.agency_website || ''}" placeholder="https://"></div>
          <div class="form-group"><label>Phone</label><input id="br-phone" class="form-control" value="${b.agency_phone || ''}"></div>
        </div>
      </div>

      <div class="card" style="margin-top:16px;padding:20px">
        <h3 style="margin-bottom:12px">Preview — Candidate Email</h3>
        <div id="br-preview" style="border:1px solid #e5e7eb;border-radius:8px;overflow:hidden;max-width:600px">
          <div style="background:${b.brand_color};padding:16px 24px;">
            <strong style="color:#000;font-size:18px" id="br-prev-name">${b.white_label_enabled ? (b.candidate_brand_name || b.agency_name) : 'ChannelView'}</strong>
          </div>
          <div style="padding:20px;background:#f9f9f9">
            <p>Hi Candidate, you've been invited to complete a video interview.</p>
            <div style="text-align:center;margin:16px 0">
              <span id="br-prev-btn" style="display:inline-block;background:${b.brand_color};color:#000;font-weight:700;padding:10px 28px;border-radius:6px">Start Interview</span>
            </div>
          </div>
        </div>
      </div>

      <div style="margin-top:20px;display:flex;gap:12px">
        <button class="btn btn-primary" onclick="saveBranding()">Save Branding</button>
      </div>`;

    // Live preview updates
    setTimeout(() => {
      const ci = document.getElementById('br-color');
      if (ci) ci.addEventListener('input', e => {
        document.getElementById('br-color-hex').textContent = e.target.value;
        document.getElementById('br-prev-btn').style.background = e.target.value;
        document.querySelector('#br-preview > div:first-child').style.background = e.target.value;
      });
      const si = document.getElementById('br-secondary');
      if (si) si.addEventListener('input', e => { document.getElementById('br-sec-hex').textContent = e.target.value; });
      const wl = document.getElementById('br-wl-enabled');
      if (wl) wl.addEventListener('change', () => {
        const name = wl.checked ? (document.getElementById('br-brand-name').value || document.getElementById('br-name').value) : 'ChannelView';
        document.getElementById('br-prev-name').textContent = name;
      });
    }, 50);
  } catch (err) {
    content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${err.message}</p></div>`;
  }
}

async function saveBranding() {
  const logoFile = document.getElementById('br-logo-file')?.files[0];
  if (logoFile) {
    const fd = new FormData();
    fd.append('logo', logoFile);
    await api('/api/branding/logo', { method: 'POST', body: fd, rawBody: true });
  }
  await api('/api/branding', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
    brand_color: document.getElementById('br-color')?.value,
    brand_secondary_color: document.getElementById('br-secondary')?.value,
    white_label_enabled: document.getElementById('br-wl-enabled')?.checked ? 1 : 0,
    candidate_brand_name: document.getElementById('br-brand-name')?.value,
    agency_name: document.getElementById('br-name')?.value,
    agency_website: document.getElementById('br-website')?.value,
    agency_phone: document.getElementById('br-phone')?.value,
  })});
  toast('Branding saved!', 'success');
}


// ==================== WORKFLOW AUTOMATION ====================

async function renderAutomation() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading automation settings...</div>';
  try {
    const settings = await api('/api/automation/settings');
    const aa = settings.auto_advance || {};
    const ar = settings.auto_reject || {};
    const rs = settings.reminder_sequence || {};

    content.innerHTML = `
      <div class="page-header">
        <div><h1>Auto-Pilot</h1><p class="subtitle">Set up automatic scoring, candidate follow-ups, and reminders</p></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div class="card">
          <h3 style="margin-bottom:16px">Auto-Score on Completion</h3>
          <p style="font-size:13px;color:#666;margin-bottom:12px">Automatically run AI scoring when a candidate completes their interview.</p>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" id="at-auto-score" ${settings.auto_score ? 'checked' : ''}>
            <span>Enable auto-scoring</span>
          </label>
        </div>

        <div class="card">
          <h3 style="margin-bottom:16px">Auto-Advance Candidates</h3>
          <p style="font-size:13px;color:#666;margin-bottom:12px">Automatically advance candidates who score above a threshold.</p>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:12px">
            <input type="checkbox" id="at-advance-on" ${aa.enabled ? 'checked' : ''}>
            <span>Enable auto-advance</span>
          </label>
          <div class="form-group"><label>Score Threshold (advance if score &ge;)</label>
            <input type="number" id="at-advance-thresh" class="form-control" value="${aa.threshold || 80}" min="0" max="100" style="width:120px">
          </div>
        </div>

        <div class="card">
          <h3 style="margin-bottom:16px">Auto-Reject Candidates</h3>
          <p style="font-size:13px;color:#666;margin-bottom:12px">Automatically flag candidates who score below a threshold.</p>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:12px">
            <input type="checkbox" id="at-reject-on" ${ar.enabled ? 'checked' : ''}>
            <span>Enable auto-reject</span>
          </label>
          <div class="form-group"><label>Score Threshold (reject if score &lt;)</label>
            <input type="number" id="at-reject-thresh" class="form-control" value="${ar.threshold || 40}" min="0" max="100" style="width:120px">
          </div>
        </div>

        <div class="card">
          <h3 style="margin-bottom:16px">Reminder Sequence</h3>
          <p style="font-size:13px;color:#666;margin-bottom:12px">Send automatic reminders to candidates who haven't completed their interview.</p>
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;margin-bottom:12px">
            <input type="checkbox" id="at-remind-on" ${rs.enabled ? 'checked' : ''}>
            <span>Enable reminder sequence</span>
          </label>
          <div style="display:flex;gap:16px;flex-wrap:wrap">
            <label><input type="checkbox" id="at-r3" ${rs.day_3 ? 'checked' : ''}> Day 3</label>
            <label><input type="checkbox" id="at-r5" ${rs.day_5 ? 'checked' : ''}> Day 5</label>
            <label><input type="checkbox" id="at-r7" ${rs.day_7 ? 'checked' : ''}> Day 7</label>
          </div>
        </div>
      </div>

      <div style="margin-top:20px">
        <button class="btn btn-primary" onclick="saveAutomation()">Save Automation Settings</button>
      </div>`;
  } catch (err) {
    content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${err.message}</p></div>`;
  }
}

async function saveAutomation() {
  await api('/api/automation/settings', { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({
    auto_score: document.getElementById('at-auto-score')?.checked || false,
    auto_advance_enabled: document.getElementById('at-advance-on')?.checked || false,
    auto_advance_threshold: parseInt(document.getElementById('at-advance-thresh')?.value || 80),
    auto_reject_enabled: document.getElementById('at-reject-on')?.checked || false,
    auto_reject_threshold: parseInt(document.getElementById('at-reject-thresh')?.value || 40),
    reminder_sequence_enabled: document.getElementById('at-remind-on')?.checked || false,
    reminder_day_3: document.getElementById('at-r3')?.checked || false,
    reminder_day_5: document.getElementById('at-r5')?.checked || false,
    reminder_day_7: document.getElementById('at-r7')?.checked || false,
  })});
  toast('Automation settings saved!', 'success');
}


// ==================== API DOCS PAGE ====================

async function renderApiDocs() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading API settings...</div>';
  try {
    const keyStatus = await api('/api/keys');

    content.innerHTML = `
      <div class="page-header">
        <div><h1>Connections</h1><p class="subtitle">Link ChannelView to the other tools you use</p></div>
        <div class="page-actions">
          <a href="/api/docs" target="_blank" class="btn btn-outline">View API Docs</a>
        </div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">API Key</h3>
        ${keyStatus.has_key ? `
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
            <code style="background:#f3f4f6;padding:8px 16px;border-radius:6px;font-size:15px">${keyStatus.prefix}</code>
            <span style="color:#666;font-size:13px">Created ${new Date(keyStatus.created_at).toLocaleDateString()}</span>
          </div>
          <div style="display:flex;gap:12px">
            <button class="btn btn-primary" onclick="regenerateApiKey()">Regenerate Key</button>
            <button class="btn btn-outline" style="color:#dc2626;border-color:#dc2626" onclick="revokeApiKey()">Revoke Key</button>
          </div>
        ` : `
          <p style="color:#666;margin-bottom:16px">No API key configured. Generate one to use the REST API.</p>
          <button class="btn btn-primary" onclick="generateApiKey()">Generate API Key</button>
        `}
      </div>

      <div class="card" style="margin-top:16px">
        <h3 style="margin-bottom:12px">Quick Start</h3>
        <p style="font-size:14px;color:#666;margin-bottom:12px">Use the API to list interviews, create candidates, and retrieve scores programmatically.</p>
        <pre style="background:#111;color:#f0f0f0;padding:16px;border-radius:8px;font-size:13px;overflow-x:auto">curl -H "X-API-Key: YOUR_KEY" ${window.location.origin}/api/v1/interviews</pre>
      </div>`;
  } catch (err) {
    content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${err.message}</p></div>`;
  }
}

async function generateApiKey() {
  const res = await api('/api/keys', { method: 'POST' });
  if (res.api_key) {
    toast('API key generated! Copy it now - it will not be shown again.', 'success');
    prompt('Your API Key (save this now):', res.api_key);
    renderApiDocs();
  }
}

async function regenerateApiKey() {
  if (!confirm('This will invalidate your current API key. Continue?')) return;
  await generateApiKey();
}

async function revokeApiKey() {
  if (!confirm('Revoke your API key? Any integrations using it will stop working.')) return;
  await api('/api/keys', { method: 'DELETE' });
  toast('API key revoked', 'success');
  renderApiDocs();
}


// ==================== INTEGRATIONS (Cycle 12) ====================

async function renderIntegrations() {
  const [zapierConfig, integrations, eventTypes] = await Promise.all([
    api('/api/integrations/zapier'),
    api('/api/integrations'),
    api('/api/integrations/events/types')
  ]);

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Connections</h1>
        <p style="color:#666;margin-top:4px">Link ChannelView with the other tools your agency uses</p>
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3 style="margin-bottom:16px">Zapier Integration</h3>
      <p style="color:#666;font-size:14px;margin-bottom:16px">Connect to 5,000+ apps via Zapier. Paste your Zapier webhook URL to automatically send candidate events.</p>
      <div style="display:flex;gap:12px;margin-bottom:16px">
        <input id="zapier-url" type="url" class="form-input" style="flex:1" placeholder="https://hooks.zapier.com/hooks/catch/..." value="${zapierConfig.zapier_webhook_url || ''}">
        <button class="btn btn-primary" onclick="saveZapierConfig()">Save</button>
        <button class="btn btn-outline" onclick="testZapier()">Test</button>
      </div>
      <label style="display:flex;align-items:center;gap:8px;font-size:14px;color:#666">
        <input type="checkbox" id="events-enabled" ${zapierConfig.events_enabled ? 'checked' : ''}> Enable event delivery
      </label>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3 style="margin-bottom:16px">Available Event Types</h3>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:12px">
        ${(eventTypes.event_types || []).map(et => `
          <div style="background:#f9fafb;border-radius:8px;padding:12px">
            <code style="font-size:13px;color:var(--brand)">${et.type}</code>
            <p style="font-size:13px;color:#666;margin-top:4px">${et.description}</p>
          </div>
        `).join('')}
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3 style="margin-bottom:16px">Connected Integrations</h3>
      ${(integrations.integrations || []).length === 0 ? `
        <p style="color:#666;font-size:14px">No integrations configured yet. Use the Zapier connection above or the Public API for custom integrations.</p>
      ` : `
        <div style="display:grid;gap:12px">
          ${(integrations.integrations || []).map(i => `
            <div style="display:flex;justify-content:space-between;align-items:center;background:#f9fafb;padding:12px 16px;border-radius:8px">
              <div>
                <span style="font-weight:600">${i.provider}</span>
                <span style="color:#666;font-size:13px;margin-left:8px">${i.active ? 'Active' : 'Inactive'}</span>
              </div>
              <button class="btn btn-outline" style="font-size:13px;color:#dc2626;border-color:#dc2626" onclick="deleteIntegration('${i.id}')">Remove</button>
            </div>
          `).join('')}
        </div>
      `}
    </div>

    <div class="card">
      <h3 style="margin-bottom:12px">Recent Events</h3>
      <div id="events-list">Loading events...</div>
    </div>`;

  // Load recent events
  const events = await api('/api/integrations/events?limit=20');
  const el = document.getElementById('events-list');
  if ((events.events || []).length === 0) {
    el.innerHTML = '<p style="color:#666;font-size:14px">No events yet. Events are emitted when candidates are invited, start, complete, or get scored.</p>';
  } else {
    el.innerHTML = `<div style="max-height:300px;overflow-y:auto">${(events.events || []).map(e => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f3f4f6">
        <div>
          <code style="font-size:12px;color:var(--brand)">${e.event_type}</code>
          <span style="color:#999;font-size:12px;margin-left:8px">${new Date(e.created_at).toLocaleString()}</span>
        </div>
        <span style="font-size:12px;color:${e.delivered ? '#16a34a' : '#999'}">${e.delivered ? 'Delivered' : 'Pending'}</span>
      </div>
    `).join('')}</div>`;
  }
}

async function saveZapierConfig() {
  await api('/api/integrations/zapier', {
    method: 'PUT',
    body: JSON.stringify({
      zapier_webhook_url: document.getElementById('zapier-url').value,
      events_enabled: document.getElementById('events-enabled').checked
    })
  });
  toast('Zapier configuration saved', 'success');
}

async function testZapier() {
  const res = await api('/api/integrations/zapier/test', { method: 'POST' });
  if (res.success) toast('Test event sent successfully!', 'success');
  else toast(res.error || 'Test failed', 'error');
}

async function deleteIntegration(id) {
  if (!confirm('Remove this integration?')) return;
  await api('/api/integrations/' + id, { method: 'DELETE' });
  toast('Integration removed', 'success');
  renderIntegrations();
}


// ==================== COMPLIANCE (Cycle 12) ====================

async function renderCompliance() {
  const [retention, log] = await Promise.all([
    api('/api/compliance/retention'),
    api('/api/compliance/log?limit=50')
  ]);

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Compliance</h1>
        <p style="color:#666;margin-top:4px">Track who did what, keep records, and stay compliant</p>
      </div>
      <div style="display:flex;gap:12px">
        <button class="btn btn-outline" onclick="exportComplianceLog()">Export CSV</button>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:16px;margin-bottom:24px">
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--brand)">${retention.default_retention_days}</div>
        <div style="color:#666;font-size:13px">Retention Days</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:${retention.eeoc_mode ? 'var(--brand)' : '#999'}">${retention.eeoc_mode ? 'ON' : 'OFF'}</div>
        <div style="color:#666;font-size:13px">EEOC Mode</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--brand)">${(log.entries || []).length}</div>
        <div style="color:#666;font-size:13px">Recent Events</div>
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3 style="margin-bottom:16px">Data Retention Settings</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px">
        <div>
          <label style="font-size:14px;font-weight:600">Default Retention Period (days)</label>
          <input id="retention-days" type="number" class="form-input" value="${retention.default_retention_days}" min="30" max="3650">
        </div>
        <div>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;margin-top:24px">
            <input type="checkbox" id="eeoc-mode" ${retention.eeoc_mode ? 'checked' : ''}>
            <span style="font-weight:600">EEOC Compliance Mode</span>
          </label>
          <p style="color:#666;font-size:12px;margin-top:4px">Requires documented justification for all scoring decisions</p>
        </div>
      </div>
      <button class="btn btn-primary" onclick="saveRetention()">Save Settings</button>
    </div>

    <div class="card">
      <h3 style="margin-bottom:16px">Audit Trail</h3>
      <div style="max-height:400px;overflow-y:auto">
        ${(log.entries || []).length === 0 ? '<p style="color:#666;font-size:14px">No audit entries yet.</p>' :
          `<table style="width:100%;font-size:13px;border-collapse:collapse">
            <thead>
              <tr style="text-align:left;border-bottom:2px solid #e5e7eb">
                <th style="padding:8px 4px">Time</th>
                <th style="padding:8px 4px">Action</th>
                <th style="padding:8px 4px">Resource</th>
                <th style="padding:8px 4px">IP</th>
              </tr>
            </thead>
            <tbody>
              ${(log.entries || []).map(e => `
                <tr style="border-bottom:1px solid #f3f4f6">
                  <td style="padding:6px 4px;color:#666">${new Date(e.created_at).toLocaleString()}</td>
                  <td style="padding:6px 4px"><code style="font-size:12px">${e.action}</code></td>
                  <td style="padding:6px 4px">${e.resource_type}${e.resource_id ? ' / ' + e.resource_id.substring(0,8) + '...' : ''}</td>
                  <td style="padding:6px 4px;color:#999">${e.ip_address || '-'}</td>
                </tr>
              `).join('')}
            </tbody>
          </table>`
        }
      </div>
    </div>`;
}

async function saveRetention() {
  await api('/api/compliance/retention', {
    method: 'PUT',
    body: JSON.stringify({
      retention_days: parseInt(document.getElementById('retention-days').value),
      eeoc_mode: document.getElementById('eeoc-mode').checked
    })
  });
  toast('Retention settings saved', 'success');
}

async function exportComplianceLog() {
  window.open('/api/compliance/export', '_blank');
}


// ==================== KANBAN BOARD (Cycle 12) ====================

async function renderKanban() {
  const interviews = await api('/api/interviews');
  const interviewId = new URLSearchParams(window.location.search).get('interview_id') || '';
  const pipeline = await api('/api/candidates/pipeline' + (interviewId ? '?interview_id=' + interviewId : ''));

  const stages = pipeline.stages || ['new', 'in_review', 'shortlisted', 'interview_scheduled', 'offered', 'hired', 'rejected'];
  const stageLabels = {
    new: 'New', in_review: 'In Review', shortlisted: 'Shortlisted',
    interview_scheduled: 'Scheduled', offered: 'Offered', hired: 'Hired', rejected: 'Rejected'
  };
  const stageColors = {
    new: '#6b7280', in_review: '#3b82f6', shortlisted: '#8b5cf6',
    interview_scheduled: '#f59e0b', offered: '#0ace0a', hired: '#16a34a', rejected: '#dc2626'
  };

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Hiring Board</h1>
        <p style="color:#666;margin-top:4px">Drag candidates between stages to move them along</p>
      </div>
      <select id="kanban-filter" class="form-input" style="width:auto" onchange="filterKanban()">
        <option value="">All Interviews</option>
        ${(interviews || []).map(i => `<option value="${i.id}" ${i.id === interviewId ? 'selected' : ''}>${i.title}</option>`).join('')}
      </select>
    </div>

    <div style="display:flex;gap:12px;overflow-x:auto;padding-bottom:16px;min-height:500px">
      ${stages.map(stage => {
        const items = (pipeline.pipeline || {})[stage] || [];
        return `
          <div class="kanban-column" data-stage="${stage}" style="min-width:220px;max-width:260px;flex:1;background:#f9fafb;border-radius:12px;padding:12px" ondragover="event.preventDefault();this.style.background='#e6fce6'" ondragleave="this.style.background='#f9fafb'" ondrop="dropCandidate(event,'${stage}');this.style.background='#f9fafb'">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <span style="font-weight:700;font-size:13px;color:${stageColors[stage]}">${stageLabels[stage] || stage}</span>
              <span style="background:${stageColors[stage]}22;color:${stageColors[stage]};font-size:12px;padding:2px 8px;border-radius:10px;font-weight:600">${items.length}</span>
            </div>
            ${items.map(c => `
              <div draggable="true" ondragstart="event.dataTransfer.setData('text/plain','${c.id}')" style="background:white;border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:grab;box-shadow:0 1px 3px rgba(0,0,0,0.08);border-left:3px solid ${stageColors[stage]}">
                <div style="font-weight:600;font-size:14px">${c.first_name} ${c.last_name}</div>
                <div style="font-size:12px;color:#666;margin-top:2px">${c.email}</div>
                ${c.ai_score ? `<div style="font-size:12px;color:var(--brand);margin-top:4px;font-weight:600">Score: ${c.ai_score}</div>` : ''}
              </div>
            `).join('')}
            ${items.length === 0 ? '<p style="text-align:center;color:#ccc;font-size:13px;margin-top:20px">No candidates</p>' : ''}
          </div>`;
      }).join('')}
    </div>`;
}

async function dropCandidate(event, newStage) {
  event.preventDefault();
  const candidateId = event.dataTransfer.getData('text/plain');
  if (!candidateId) return;
  await api('/api/candidates/' + candidateId + '/pipeline-stage', {
    method: 'PUT',
    body: JSON.stringify({ stage: newStage, order: 0 })
  });
  toast('Moved to ' + newStage.replace(/_/g, ' '), 'success');
  renderKanban();
}

function filterKanban() {
  const interviewId = document.getElementById('kanban-filter').value;
  if (interviewId) {
    history.replaceState(null, '', '/kanban?interview_id=' + interviewId);
  } else {
    history.replaceState(null, '', '/kanban');
  }
  renderKanban();
}


// ==================== SYSTEM (Cycle 13) ====================

async function renderSystem() {
  const [dbStats, perf, emailStats] = await Promise.all([
    api('/api/system/db-stats'),
    api('/api/system/performance'),
    api('/api/email/stats')
  ]);

  const stats = dbStats.stats || {};
  const metrics = perf.metrics || {};

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
      <div>
        <h1 style="font-size:24px;font-weight:700">System Monitor</h1>
        <p style="color:#666;margin-top:4px">Database health, performance metrics, and email delivery</p>
      </div>
      <div style="display:flex;gap:12px">
        <button class="btn btn-outline" onclick="createBackup()">Create Backup</button>
        <button class="btn btn-outline" onclick="runTenantAudit()">Tenant Audit</button>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:16px;margin-bottom:24px">
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--brand)">${stats.db_size_mb || 0}</div>
        <div style="color:#666;font-size:13px">DB Size (MB)</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:${stats.integrity === 'ok' ? 'var(--brand)' : '#dc2626'}">${stats.integrity || '?'}</div>
        <div style="color:#666;font-size:13px">DB Integrity</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--brand)">${stats.journal_mode || '?'}</div>
        <div style="color:#666;font-size:13px">Journal Mode</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:var(--brand)">${emailStats.delivery_rate || 100}%</div>
        <div style="color:#666;font-size:13px">Email Delivery</div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
      <div class="card">
        <h3 style="margin-bottom:16px">Table Row Counts</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px">
          ${Object.entries(stats).filter(([k]) => !['db_size_mb','journal_mode','integrity'].includes(k)).map(([k, v]) => `
            <div style="display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #f3f4f6">
              <span style="font-size:13px;color:#666">${k}</span>
              <span style="font-size:13px;font-weight:600">${v}</span>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">Performance (7 days)</h3>
        <div style="margin-bottom:16px">
          <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6">
            <span style="color:#666">New Candidates</span>
            <span style="font-weight:600">${(metrics.last_7_days || {}).new_candidates || 0}</span>
          </div>
          <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6">
            <span style="color:#666">Completions</span>
            <span style="font-weight:600">${(metrics.last_7_days || {}).completions || 0}</span>
          </div>
          <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6">
            <span style="color:#666">Storage Used</span>
            <span style="font-weight:600">${metrics.storage_used_mb || 0} MB</span>
          </div>
          <div style="display:flex;justify-content:space-between;padding:8px 0">
            <span style="color:#666">Rate Limit Entries</span>
            <span style="font-weight:600">${metrics.rate_limit_entries || 0}</span>
          </div>
        </div>

        <h4 style="margin-bottom:8px;font-size:14px">Top Interviews by Candidates</h4>
        ${(metrics.top_interviews || []).map(i => `
          <div style="display:flex;justify-content:space-between;padding:4px 0;font-size:13px">
            <span style="color:#666">${i.title}</span>
            <span style="font-weight:600">${i.candidates}</span>
          </div>
        `).join('') || '<p style="color:#999;font-size:13px">No data yet</p>'}
      </div>
    </div>

    <div class="card" style="margin-bottom:20px">
      <h3 style="margin-bottom:16px">Email Delivery</h3>
      <div style="display:flex;gap:24px;margin-bottom:16px">
        <div><span style="font-size:24px;font-weight:700;color:var(--brand)">${emailStats.sent || 0}</span> <span style="color:#666;font-size:13px">Sent</span></div>
        <div><span style="font-size:24px;font-weight:700;color:#dc2626">${emailStats.failed || 0}</span> <span style="color:#666;font-size:13px">Failed</span></div>
        <div><span style="font-size:24px;font-weight:700">${emailStats.total || 0}</span> <span style="color:#666;font-size:13px">Total</span></div>
      </div>
      <div style="display:flex;gap:12px">
        <input id="test-email-addr" type="email" class="form-input" style="flex:1" placeholder="Email to test delivery..." value="">
        <button class="btn btn-primary" onclick="testEmailDelivery()">Test Delivery</button>
      </div>
    </div>

    <div id="audit-result"></div>`;
}

async function createBackup() {
  const res = await api('/api/system/backup', { method: 'POST' });
  if (res.success) toast('Backup created: ' + res.size_mb + 'MB', 'success');
  else toast(res.error || 'Backup failed', 'error');
}

async function runTenantAudit() {
  const res = await api('/api/system/tenant-audit');
  const el = document.getElementById('audit-result');
  if (res.clean) {
    el.innerHTML = '<div class="card" style="border-left:4px solid var(--brand)"><h3>Tenant Audit: Clean</h3><p style="color:#666;font-size:14px">No cross-tenant data leaks detected.</p></div>';
  } else {
    el.innerHTML = '<div class="card" style="border-left:4px solid #dc2626"><h3>Tenant Audit: Issues Found</h3><ul>' + (res.issues || []).map(i => '<li style="color:#dc2626">' + i + '</li>').join('') + '</ul></div>';
  }
}

async function testEmailDelivery() {
  const email = document.getElementById('test-email-addr').value;
  if (!email) { toast('Enter an email address', 'error'); return; }
  const res = await api('/api/email/test-deliverability', {
    method: 'POST',
    body: JSON.stringify({ to_email: email })
  });
  if (res.success) toast('Test email sent to ' + email, 'success');
  else toast(res.error || 'Delivery test failed', 'error');
}


// ==================== CYCLE 14: REPORTS ====================

async function renderReports() {
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Reports</h1><p class="subtitle">Hiring funnel, candidate scorecards, and side-by-side comparisons</p></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card">
        <h3 style="margin-bottom:16px">Hiring Funnel</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">
          <div class="form-group"><label>From</label><input type="date" id="rpt-from" class="form-control"></div>
          <div class="form-group"><label>To</label><input type="date" id="rpt-to" class="form-control"></div>
        </div>
        <button class="btn btn-primary" onclick="loadFunnelReport()">Generate Hiring Report</button>
        <div id="funnel-result" style="margin-top:16px"></div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">Candidate Comparison</h3>
        <p style="font-size:13px;color:#666;margin-bottom:12px">Select candidates to compare side by side.</p>
        <div id="comparison-candidates" style="margin-bottom:12px"></div>
        <button class="btn btn-outline" onclick="loadComparisonCandidates()">Load Candidates</button>
        <button class="btn btn-primary" onclick="runComparison()" style="margin-left:8px">Compare Selected</button>
        <div id="comparison-result" style="margin-top:16px"></div>
      </div>
    </div>

    <div class="card">
      <h3 style="margin-bottom:16px">Saved Report Configs</h3>
      <div id="saved-configs" style="margin-bottom:16px"><span style="color:#999;font-size:13px">Loading...</span></div>
      <div style="display:flex;gap:8px;align-items:center">
        <input id="rpt-config-title" class="form-control" placeholder="Report title" style="max-width:250px">
        <select id="rpt-config-type" class="form-control" style="max-width:150px"><option value="funnel">Funnel</option><option value="comparison">Comparison</option><option value="scorecard">Scorecard</option></select>
        <button class="btn btn-outline" onclick="saveReportConfig()">Save Config</button>
      </div>
    </div>`;

  loadSavedConfigs();
}

async function loadFunnelReport() {
  const from = document.getElementById('rpt-from').value;
  const to = document.getElementById('rpt-to').value;
  const params = new URLSearchParams();
  if (from) params.set('from', from);
  if (to) params.set('to', to);
  const res = await api('/api/reports/funnel?' + params.toString());
  const f = res.funnel;
  if (!f) { toast('Failed to load funnel', 'error'); return; }

  const el = document.getElementById('funnel-result');
  const stages = Object.entries(f.by_status).filter(([,v]) => v > 0);
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:12px">
      <div style="background:#f0fdf4;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--primary)">${f.total_candidates}</div><div style="font-size:12px;color:#666">Total</div></div>
      <div style="background:#f0fdf4;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--primary)">${f.completion_rate}%</div><div style="font-size:12px;color:#666">Completion</div></div>
      <div style="background:#f0fdf4;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--primary)">${f.avg_score || 'N/A'}</div><div style="font-size:12px;color:#666">Avg Score</div></div>
    </div>
    ${stages.length ? '<div style="display:flex;gap:4px;height:28px;border-radius:6px;overflow:hidden">' + stages.map(([k,v]) => {
      const colors = {invited:'#3b82f6',started:'#f59e0b',completed:'#10b981',reviewed:'#8b5cf6',shortlisted:'#06b6d4',hired:'#22c55e',rejected:'#ef4444'};
      return `<div title="${k}: ${v}" style="flex:${v};background:${colors[k]||'#666'};display:flex;align-items:center;justify-content:center;color:#fff;font-size:11px;font-weight:600">${v}</div>`;
    }).join('') + '</div><div style="display:flex;flex-wrap:wrap;gap:8px;margin-top:8px">' + stages.map(([k,v]) => {
      const colors = {invited:'#3b82f6',started:'#f59e0b',completed:'#10b981',reviewed:'#8b5cf6',shortlisted:'#06b6d4',hired:'#22c55e',rejected:'#ef4444'};
      return `<span style="font-size:11px;color:#666"><span style="display:inline-block;width:8px;height:8px;background:${colors[k]||'#666'};border-radius:50%;margin-right:4px"></span>${k} (${v})</span>`;
    }).join('') + '</div>' : '<p style="color:#999;font-size:13px">No candidates in date range</p>'}`;
}

async function loadComparisonCandidates() {
  const res = await api('/api/candidates');
  const el = document.getElementById('comparison-candidates');
  if (!res.candidates || res.candidates.length === 0) {
    el.innerHTML = '<p style="color:#999;font-size:13px">No candidates found</p>';
    return;
  }
  el.innerHTML = res.candidates.slice(0, 20).map(c =>
    `<label style="display:flex;align-items:center;gap:6px;font-size:13px;margin-bottom:4px"><input type="checkbox" class="cmp-check" value="${c.id}"> ${c.first_name} ${c.last_name} ${c.ai_score ? '(' + c.ai_score + ')' : ''}</label>`
  ).join('');
}

async function runComparison() {
  const ids = [...document.querySelectorAll('.cmp-check:checked')].map(cb => cb.value);
  if (ids.length < 2) { toast('Select at least 2 candidates', 'error'); return; }
  const res = await api('/api/reports/comparison', { method: 'POST', body: JSON.stringify({ candidate_ids: ids }) });
  const el = document.getElementById('comparison-result');
  if (!res.comparison || res.comparison.length === 0) { el.innerHTML = '<p style="color:#999">No data</p>'; return; }
  el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px;margin-top:8px">
    <tr style="background:#f3f4f6"><th style="padding:8px;text-align:left">Candidate</th><th>Score</th><th>Status</th><th>Stage</th></tr>
    ${res.comparison.map(c => `<tr style="border-top:1px solid #e5e7eb"><td style="padding:8px;font-weight:600">${c.name}</td><td style="text-align:center">${c.overall_score || 'N/A'}</td><td style="text-align:center">${c.status}</td><td style="text-align:center">${c.pipeline_stage}</td></tr>`).join('')}
  </table>`;
}

async function loadSavedConfigs() {
  const res = await api('/api/reports/configs');
  const el = document.getElementById('saved-configs');
  if (!res.configs || res.configs.length === 0) {
    el.innerHTML = '<p style="color:#999;font-size:13px">No saved report configurations.</p>';
    return;
  }
  el.innerHTML = res.configs.map(c => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;margin-bottom:6px">
      <div><strong style="font-size:14px">${c.title}</strong> <span style="font-size:12px;color:#666;background:#f3f4f6;padding:2px 8px;border-radius:4px;margin-left:6px">${c.report_type}</span></div>
      <button class="btn btn-sm btn-outline" onclick="deleteReportConfig('${c.id}')" style="font-size:11px;color:#dc2626;border-color:#dc2626">Delete</button>
    </div>`).join('');
}

async function saveReportConfig() {
  const title = document.getElementById('rpt-config-title').value;
  const type = document.getElementById('rpt-config-type').value;
  if (!title) { toast('Enter a report title', 'error'); return; }
  await api('/api/reports/configs', { method: 'POST', body: JSON.stringify({ title, report_type: type, config: {} }) });
  toast('Config saved', 'success');
  loadSavedConfigs();
}

async function deleteReportConfig(id) {
  await api('/api/reports/configs/' + id, { method: 'DELETE' });
  toast('Config deleted', 'success');
  loadSavedConfigs();
}


// ==================== CYCLE 14: SECURITY ====================

async function renderSecurity() {
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Security</h1><p class="subtitle">Password, login history, and account safety</p></div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card">
        <h3 style="margin-bottom:16px">Security Audit</h3>
        <button class="btn btn-primary" onclick="runSecurityAudit()">Run Audit</button>
        <div id="audit-result-sec" style="margin-top:16px"></div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">Change Password</h3>
        <div class="form-group"><label>Current Password</label><input type="password" id="sec-cur-pw" class="form-control"></div>
        <div class="form-group"><label>New Password</label><input type="password" id="sec-new-pw" class="form-control"></div>
        <div class="form-group"><label>Confirm New Password</label><input type="password" id="sec-confirm-pw" class="form-control"></div>
        <button class="btn btn-primary" onclick="changePassword()">Update Password</button>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card">
        <h3 style="margin-bottom:16px">Password Policy</h3>
        <div id="pw-policy"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">Active Sessions</h3>
        <div id="active-sessions"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
      <div class="card">
        <h3 style="margin-bottom:16px">Input Sanitization Test</h3>
        <div class="form-group"><label>Test Input</label><input id="sec-test-input" class="form-control" placeholder='<script>alert("XSS")</script>'></div>
        <button class="btn btn-outline" onclick="testSanitize()">Test</button>
        <div id="sanitize-result" style="margin-top:12px"></div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:16px">Recent Security Events</h3>
        <div id="security-events"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>`;

  loadPasswordPolicy();
  loadActiveSessions();
  loadSecurityEvents();
}

async function runSecurityAudit() {
  const res = await api('/api/security/audit');
  const a = res.audit;
  if (!a) { toast('Audit failed', 'error'); return; }
  const el = document.getElementById('audit-result-sec');
  const gradeColor = {A:'#22c55e',B:'#84cc16',C:'#f59e0b',D:'#ef4444'}[a.grade] || '#666';
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:16px;margin-bottom:16px">
      <div style="width:64px;height:64px;border-radius:50%;background:${gradeColor};display:flex;align-items:center;justify-content:center;font-size:28px;font-weight:700;color:#fff">${a.grade}</div>
      <div><div style="font-size:20px;font-weight:700">${a.score}/${a.total} checks passed</div><div style="color:#666;font-size:13px">${a.pct}% security score</div></div>
    </div>
    ${a.checks.map(c => `<div style="display:flex;align-items:center;gap:8px;padding:6px 0;border-top:1px solid #f3f4f6">
      <span style="font-size:16px">${c.status === 'pass' ? '&#x2705;' : c.status === 'warn' ? '&#x26A0;&#xFE0F;' : '&#x2139;&#xFE0F;'}</span>
      <div><div style="font-size:13px;font-weight:600">${c.name}</div>${c.message ? `<div style="font-size:12px;color:#666">${c.message}</div>` : ''}</div>
    </div>`).join('')}`;
}

async function changePassword() {
  const cur = document.getElementById('sec-cur-pw').value;
  const newPw = document.getElementById('sec-new-pw').value;
  const confirm = document.getElementById('sec-confirm-pw').value;
  if (!cur || !newPw) { toast('All fields required', 'error'); return; }
  if (newPw !== confirm) { toast('Passwords do not match', 'error'); return; }
  if (newPw.length < 8) { toast('Password must be at least 8 characters', 'error'); return; }
  const res = await api('/api/security/update-password', { method: 'POST', body: JSON.stringify({ current_password: cur, new_password: newPw }) });
  if (res.success) { toast('Password updated', 'success'); document.getElementById('sec-cur-pw').value = ''; document.getElementById('sec-new-pw').value = ''; document.getElementById('sec-confirm-pw').value = ''; }
  else toast(res.error || 'Failed', 'error');
}

async function loadPasswordPolicy() {
  const res = await api('/api/security/password-rules');
  const p = res.policy;
  if (!p) return;
  document.getElementById('pw-policy').innerHTML = `
    <div style="font-size:13px;line-height:1.8">
      <div>Min length: <strong>${p.min_length}</strong></div>
      <div>Uppercase required: <strong>${p.require_uppercase ? 'Yes' : 'No'}</strong></div>
      <div>Number required: <strong>${p.require_number ? 'Yes' : 'No'}</strong></div>
      <div>Max failed attempts: <strong>${p.max_failed_attempts}</strong></div>
      <div>Lockout duration: <strong>${p.lockout_duration_minutes} min</strong></div>
      <div>Session timeout: <strong>${p.session_timeout_hours} hrs</strong></div>
      <div>MFA available: <strong>${p.mfa_available ? 'Yes' : 'No'}</strong></div>
    </div>`;
}

async function loadActiveSessions() {
  const res = await api('/api/security/sessions');
  const el = document.getElementById('active-sessions');
  if (!res.sessions) return;
  el.innerHTML = res.sessions.map(s => `
    <div style="padding:8px 0;border-bottom:1px solid #f3f4f6;font-size:13px">
      <div style="font-weight:600">${s.current ? 'Current Session' : 'Other'}</div>
      <div style="color:#666">IP: ${s.ip || 'unknown'}</div>
      <div style="color:#666">${s.last_login_at ? 'Last login: ' + new Date(s.last_login_at).toLocaleString() : ''}</div>
    </div>`).join('');
}

async function loadSecurityEvents() {
  const res = await api('/api/security/event-log?limit=10');
  const el = document.getElementById('security-events');
  if (!res.events || res.events.length === 0) {
    el.innerHTML = '<p style="color:#999;font-size:13px">No security events recorded.</p>';
    return;
  }
  el.innerHTML = res.events.map(e => `
    <div style="padding:6px 0;border-bottom:1px solid #f3f4f6;font-size:13px">
      <div style="display:flex;justify-content:space-between">
        <span style="font-weight:600">${e.event_type}</span>
        <span style="font-size:11px;padding:2px 6px;border-radius:4px;background:${e.severity==='warning'?'#fef3c7':e.severity==='critical'?'#fee2e2':'#f0fdf4'};color:${e.severity==='warning'?'#92400e':e.severity==='critical'?'#dc2626':'#166534'}">${e.severity}</span>
      </div>
      <div style="color:#666;font-size:11px">${new Date(e.created_at).toLocaleString()}</div>
    </div>`).join('');
}

async function testSanitize() {
  const input = document.getElementById('sec-test-input').value;
  if (!input) { toast('Enter test input', 'error'); return; }
  const res = await api('/api/security/input-sanitize', { method: 'POST', body: JSON.stringify({ input }) });
  const el = document.getElementById('sanitize-result');
  el.innerHTML = `
    <div style="font-size:13px;background:#f9fafb;padding:12px;border-radius:6px">
      <div style="margin-bottom:4px"><strong>Original:</strong> <code style="background:#fee2e2;padding:2px 4px;border-radius:3px">${res.original}</code></div>
      <div style="margin-bottom:4px"><strong>Sanitized:</strong> <code style="background:#f0fdf4;padding:2px 4px;border-radius:3px">${res.sanitized}</code></div>
      <div>XSS detected: <strong style="color:${res.risks?.xss_detected?'#dc2626':'#22c55e'}">${res.risks?.xss_detected?'Yes':'No'}</strong> | SQL injection: <strong style="color:${res.risks?.sql_injection_detected?'#dc2626':'#22c55e'}">${res.risks?.sql_injection_detected?'Yes':'No'}</strong></div>
    </div>`;
}


// ==================== CYCLE 15: TEAM ====================

async function renderTeam() {
  content.innerHTML = `
    <div class="page-header">
      <div><h1>My Team</h1><p class="subtitle">Add people, set their roles, and manage access</p></div>
      <button class="btn btn-primary" onclick="showInviteModal()">Invite Member</button>
    </div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px">
      <div class="card">
        <h3 style="margin-bottom:16px">Team Members</h3>
        <div id="team-list"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
      <div class="card">
        <h3 style="margin-bottom:16px">Role Permissions</h3>
        <div id="role-perms"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>
    <div id="invite-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.4);z-index:1001;display:none;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:12px;padding:24px;width:400px;max-width:90vw">
        <h3 style="margin-bottom:16px">Invite Team Member</h3>
        <div class="form-group"><label>Email</label><input id="inv-email" class="form-control" type="email" placeholder="team@agency.com"></div>
        <div class="form-group"><label>Display Name</label><input id="inv-name" class="form-control" placeholder="Jane Smith"></div>
        <div class="form-group"><label>Role</label><select id="inv-role" class="form-control"><option value="reviewer">Reviewer</option><option value="recruiter">Recruiter</option><option value="admin">Admin</option></select></div>
        <div style="display:flex;gap:8px;margin-top:16px"><button class="btn btn-primary" onclick="sendInvite()">Send Invite</button><button class="btn btn-outline" onclick="hideInviteModal()">Cancel</button></div>
      </div>
    </div>`;
  loadTeamMembers();
  loadRolePerms();
}

async function loadTeamMembers() {
  const res = await api('/api/team/members');
  const el = document.getElementById('team-list');
  if (!res.members || res.members.length === 0) {
    el.innerHTML = '<p style="color:#999;font-size:13px">No team members yet. Invite your first member above.</p>';
    return;
  }
  el.innerHTML = res.members.map(m => `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:10px 0;border-bottom:1px solid #f3f4f6">
      <div><div style="font-weight:600;font-size:14px">${m.name || m.display_name || m.email}</div><div style="font-size:12px;color:#666">${m.email} &middot; <span style="background:#f0fdf4;color:#166534;padding:2px 8px;border-radius:4px;font-size:11px">${m.role}</span></div></div>
      <div style="display:flex;gap:6px"><button class="btn btn-sm btn-outline" onclick="removeTeamMember('${m.id}')" style="font-size:11px;color:#dc2626;border-color:#dc2626">Remove</button></div>
    </div>`).join('');
}

async function loadRolePerms() {
  const res = await api('/api/team/permissions');
  const el = document.getElementById('role-perms');
  if (!res.roles) return;
  el.innerHTML = Object.entries(res.roles).map(([role, perms]) => `
    <div style="margin-bottom:12px"><div style="font-weight:600;font-size:14px;text-transform:capitalize;margin-bottom:4px">${role}</div>
    <div style="font-size:12px;color:#666">${perms.join(', ')}</div></div>`).join('');
}

function showInviteModal() { document.getElementById('invite-modal').style.display = 'flex'; }
function hideInviteModal() { document.getElementById('invite-modal').style.display = 'none'; }

async function sendInvite() {
  const email = document.getElementById('inv-email').value;
  const name = document.getElementById('inv-name').value;
  const role = document.getElementById('inv-role').value;
  if (!email) { toast('Email required', 'error'); return; }
  const res = await api('/api/team/invite', { method: 'POST', body: JSON.stringify({ email, display_name: name, role }) });
  if (res.success) { toast('Invite sent', 'success'); hideInviteModal(); loadTeamMembers(); }
  else toast(res.error || 'Failed', 'error');
}

async function removeTeamMember(id) {
  const res = await api('/api/team/members/' + id, { method: 'DELETE' });
  if (res.success) { toast('Member removed', 'success'); loadTeamMembers(); }
  else toast(res.error || 'Failed', 'error');
}


// ==================== CYCLE 15: ACTIVITY ====================

async function renderActivity() {
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Activity Feed</h1><p class="subtitle">What your team has been doing</p></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card">
        <h3 style="margin-bottom:16px">Activity Summary</h3>
        <div id="activity-summary"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
      <div class="card">
        <h3 style="margin-bottom:16px">Plan Usage</h3>
        <div id="plan-usage"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>
    <div class="card">
      <h3 style="margin-bottom:16px">Recent Activity</h3>
      <div id="activity-feed"><span style="color:#999;font-size:13px">Loading...</span></div>
    </div>`;
  loadActivitySummary();
  loadPlanUsage();
  loadActivityFeed();
}

async function loadActivitySummary() {
  const res = await api('/api/activity/audit-summary');
  const el = document.getElementById('activity-summary');
  el.innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px">
      <div style="background:#f0fdf4;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--primary)">${res.today || 0}</div><div style="font-size:12px;color:#666">Today</div></div>
      <div style="background:#f0fdf4;border-radius:8px;padding:12px;text-align:center"><div style="font-size:24px;font-weight:700;color:var(--primary)">${res.this_week || 0}</div><div style="font-size:12px;color:#666">This Week</div></div>
    </div>
    ${(res.top_actions||[]).length ? '<div style="font-size:13px;color:#666;margin-bottom:4px">Top actions:</div>' + res.top_actions.map(a => `<div style="display:flex;justify-content:space-between;font-size:13px;padding:3px 0"><span>${a.action}</span><strong>${a.count}</strong></div>`).join('') : ''}`;
}

async function loadPlanUsage() {
  const res = await api('/api/billing/usage');
  const el = document.getElementById('plan-usage');
  if (!res.plan) { el.innerHTML = '<p style="color:#999">Unable to load</p>'; return; }
  const pct = res.candidates_limit > 0 ? Math.round(res.candidates_used / res.candidates_limit * 100) : 0;
  const barColor = pct > 80 ? '#ef4444' : pct > 50 ? '#f59e0b' : 'var(--primary)';
  el.innerHTML = `
    <div style="font-size:14px;margin-bottom:12px"><strong style="text-transform:capitalize">${res.plan}</strong> plan</div>
    <div style="margin-bottom:12px"><div style="font-size:13px;color:#666;margin-bottom:4px">Candidates: ${res.candidates_used}/${res.candidates_limit > 0 ? res.candidates_limit : '∞'}</div>
      <div style="background:#e5e7eb;border-radius:4px;height:8px"><div style="background:${barColor};width:${Math.min(pct, 100)}%;height:100%;border-radius:4px;transition:width .3s"></div></div>
    </div>
    <div style="font-size:13px;color:#666">Team seats: ${res.team_seats_used}/${res.team_seats_limit > 0 ? res.team_seats_limit : '∞'}</div>
    <div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:6px">
      ${Object.entries(res.features||{}).map(([k,v]) => `<span style="font-size:11px;padding:3px 8px;border-radius:4px;background:${v?'#f0fdf4':'#fee2e2'};color:${v?'#166534':'#dc2626'}">${k.replace(/_/g,' ')}: ${v?'Yes':'No'}</span>`).join('')}
    </div>`;
}

async function loadActivityFeed() {
  const res = await api('/api/activity/feed?limit=20');
  const el = document.getElementById('activity-feed');
  if (!res.activities || res.activities.length === 0) {
    el.innerHTML = '<p style="color:#999;font-size:13px">No activity yet. Actions like inviting candidates, scoring, and team changes will appear here.</p>';
    return;
  }
  el.innerHTML = res.activities.map(a => `
    <div style="display:flex;gap:12px;padding:10px 0;border-bottom:1px solid #f3f4f6">
      <div style="width:32px;height:32px;border-radius:50%;background:var(--primary);color:#000;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:13px;flex-shrink:0">${(a.actor_name||'?')[0]}</div>
      <div><div style="font-size:13px"><strong>${a.actor_name||'System'}</strong> ${a.action.replace(/_/g,' ')}${a.entity_name ? ' — ' + a.entity_name : ''}</div>
      <div style="font-size:11px;color:#999">${new Date(a.created_at).toLocaleString()}</div></div>
    </div>`).join('');
}


// ==================== CYCLE 15: DEPLOY ====================

async function renderDeploy() {
  content.innerHTML = `
    <div class="page-header">
      <div><h1>Deployment</h1><p class="subtitle">Production readiness and deployment configuration</p></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
      <div class="card">
        <h3 style="margin-bottom:16px">Production Readiness</h3>
        <button class="btn btn-primary" onclick="checkReadiness()">Run Readiness Check</button>
        <div id="readiness-result" style="margin-top:16px"></div>
      </div>
      <div class="card">
        <h3 style="margin-bottom:16px">Current Configuration</h3>
        <div id="deploy-config"><span style="color:#999;font-size:13px">Loading...</span></div>
      </div>
    </div>
    <div class="card">
      <h3 style="margin-bottom:16px">Environment Template</h3>
      <p style="font-size:13px;color:#666;margin-bottom:12px">Copy this template to create your production .env file:</p>
      <button class="btn btn-outline" onclick="loadEnvTemplate()">Show .env Template</button>
      <pre id="env-template" style="display:none;background:#1e1e1e;color:#d4d4d4;padding:16px;border-radius:8px;font-size:12px;overflow-x:auto;margin-top:12px;white-space:pre-wrap"></pre>
    </div>`;
  loadDeployConfig();
}

async function checkReadiness() {
  const res = await api('/api/deploy/readiness');
  const el = document.getElementById('readiness-result');
  if (!res.checks) { el.innerHTML = '<p style="color:#dc2626">Check failed</p>'; return; }
  el.innerHTML = `
    <div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">
      <div style="font-size:20px;font-weight:700;color:${res.ready?'#22c55e':'#f59e0b'}">${res.ready?'Ready':'Not Ready'}</div>
      <div style="font-size:13px;color:#666">${res.score}/${res.total} checks passed (${res.pct}%)</div>
    </div>
    ${res.checks.map(c => `<div style="display:flex;align-items:center;gap:8px;padding:4px 0">
      <span style="font-size:14px">${c.status==='pass'?'&#x2705;':c.status==='warn'?'&#x26A0;&#xFE0F;':'&#x274C;'}</span>
      <span style="font-size:13px">${c.name}</span>
      ${c.message?`<span style="font-size:11px;color:#999;margin-left:auto">${c.message}</span>`:''}
    </div>`).join('')}`;
}

async function loadDeployConfig() {
  const res = await api('/api/deploy/config');
  const c = res.config;
  if (!c) return;
  document.getElementById('deploy-config').innerHTML = `
    <div style="font-size:13px;line-height:2">
      <div>Environment: <strong>${c.environment}</strong></div>
      <div>Host: <strong>${c.host}:${c.port}</strong></div>
      <div>Debug: <strong>${c.debug_mode?'Yes':'No'}</strong></div>
      <div>Secret key: <strong style="color:${c.secret_key_set?'#22c55e':'#dc2626'}">${c.secret_key_set?'Custom':'Default (unsafe)'}</strong></div>
      <div>CORS: <strong>${c.cors_origins}</strong></div>
      <div>Stripe: <strong style="color:${c.stripe_configured?'#22c55e':'#999'}">${c.stripe_configured?'Configured':'Not set'}</strong></div>
      <div>AI: <strong style="color:${c.ai_configured?'#22c55e':'#999'}">${c.ai_configured?'Configured':'Not set'}</strong></div>
      <div>Email: <strong style="color:${c.sendgrid_configured?'#22c55e':'#999'}">${c.sendgrid_configured?'Configured':'Not set'}</strong></div>
    </div>`;
}

async function loadEnvTemplate() {
  const res = await api('/api/deploy/env-template');
  const el = document.getElementById('env-template');
  el.textContent = res.template || '';
  el.style.display = 'block';
}


// ==================== CYCLE 16: ADMIN ANALYTICS ====================
async function renderAdminAnalytics() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading analytics...</div>';

  try {
    const [metricsRes, funnelRes, perfRes] = await Promise.all([
      api('GET', '/api/admin/metrics'),
      api('GET', '/api/admin/funnel'),
      api('GET', '/api/admin/interviewer-performance')
    ]);

    const m = metricsRes.overview || {};
    const funnel = funnelRes.funnel || [];
    const interviews = perfRes.interviews || [];
    const depts = metricsRes.departments || [];
    const trend = metricsRes.weekly_trend || [];
    const statuses = metricsRes.statuses || {};

    content.innerHTML = `
      <div class="page-header"><h2>Admin Analytics Dashboard</h2>
        <div class="header-actions">
          <button class="btn btn-outline" onclick="createSnapshot()">Take Snapshot</button>
          <button class="btn btn-primary" onclick="exportData()">Export CSV</button>
        </div>
      </div>

      <div class="stats-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
        <div class="stat-card"><div class="stat-value">${m.total_interviews||0}</div><div class="stat-label">Total Interviews</div></div>
        <div class="stat-card"><div class="stat-value">${m.total_candidates||0}</div><div class="stat-label">Total Candidates</div></div>
        <div class="stat-card"><div class="stat-value">${m.completion_rate||0}%</div><div class="stat-label">Completion Rate</div></div>
        <div class="stat-card"><div class="stat-value">${m.avg_score||0}</div><div class="stat-label">Avg Score</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
        <div class="card">
          <h3 style="margin-bottom:12px">Hiring Funnel</h3>
          <div id="funnel-chart">
            ${funnel.map(s => `
              <div style="display:flex;align-items:center;gap:10px;margin-bottom:8px">
                <div style="width:120px;font-size:13px;color:#666">${s.stage}</div>
                <div style="flex:1;background:#f3f4f6;border-radius:4px;height:28px;position:relative">
                  <div style="width:${s.pct}%;background:var(--primary);border-radius:4px;height:100%;transition:width .3s"></div>
                  <span style="position:absolute;right:8px;top:4px;font-size:12px;font-weight:600">${s.count} (${s.pct}%)</span>
                </div>
              </div>
            `).join('')}
          </div>
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px">Status Breakdown</h3>
          ${Object.entries(statuses).map(([k,v]) => `
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6">
              <span style="text-transform:capitalize">${k}</span><span style="font-weight:600">${v}</span>
            </div>
          `).join('')}
          ${Object.keys(statuses).length === 0 ? '<p style="color:#999">No candidates yet</p>' : ''}
        </div>
      </div>

      <div class="card" style="margin-bottom:24px">
        <h3 style="margin-bottom:12px">Interview Performance</h3>
        <table class="data-table"><thead><tr>
          <th>Interview</th><th>Department</th><th>Candidates</th><th>Completed</th><th>Completion %</th><th>Avg Score</th><th>Avg Days</th>
        </tr></thead><tbody>
          ${interviews.map(i => `<tr>
            <td>${i.title||'Untitled'}</td><td>${i.department||'-'}</td><td>${i.total_candidates}</td>
            <td>${i.completed}</td><td>${i.completion_rate}%</td>
            <td>${i.avg_score}</td><td>${i.avg_completion_days}d</td>
          </tr>`).join('')}
          ${interviews.length === 0 ? '<tr><td colspan="7" style="text-align:center;color:#999">No interviews yet</td></tr>' : ''}
        </tbody></table>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px">
        <div class="card">
          <h3 style="margin-bottom:12px">Weekly Trend</h3>
          ${trend.map(t => `
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6">
              <span>${t.week}</span><span style="font-weight:600">${t.candidates} candidates</span>
            </div>
          `).join('')}
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px">By Department</h3>
          ${depts.map(d => `
            <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6">
              <span>${d.department}</span><span style="font-weight:600">${d.count}</span>
            </div>
          `).join('')}
          ${depts.length === 0 ? '<p style="color:#999">No department data</p>' : ''}
        </div>
      </div>
    `;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error loading analytics</h3><p>${e.message}</p></div>`; }
}

async function createSnapshot() {
  const res = await api('POST', '/api/admin/snapshot');
  if (res.success) showToast('Snapshot created for ' + res.date);
  else showToast('Failed to create snapshot', 'error');
}

async function exportData() {
  const res = await api('GET', '/api/admin/export');
  if (res.csv) {
    const blob = new Blob([res.csv], {type: 'text/csv'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'channelview_export.csv'; a.click();
    showToast(`Exported ${res.total} candidates`);
  }
}


// ==================== CYCLE 16: AI SCORING ====================
async function renderAiScoring() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading AI scoring...</div>';

  try {
    const [configRes, rubricsRes] = await Promise.all([
      api('GET', '/api/ai/scoring-config'),
      api('GET', '/api/scoring/rubrics')
    ]);

    const config = configRes;
    const rubrics = rubricsRes.rubrics || [];
    const features = config.supported_features || {};

    content.innerHTML = `
      <div class="page-header"><h2>AI Scoring</h2>
        <button class="btn btn-primary" onclick="showCreateRubricModal()">Create Rubric</button>
      </div>

      <div class="stats-grid" style="display:grid;grid-template-columns:repeat(3,1fr);gap:16px;margin-bottom:24px">
        <div class="stat-card">
          <div class="stat-value" style="color:${config.ai_available ? 'var(--primary)' : '#dc2626'}">${config.ai_available ? 'Active' : 'Mock Mode'}</div>
          <div class="stat-label">AI Engine Status</div>
        </div>
        <div class="stat-card"><div class="stat-value">${rubrics.length}</div><div class="stat-label">Custom Rubrics</div></div>
        <div class="stat-card"><div class="stat-value">${Object.values(features).filter(Boolean).length}/${Object.keys(features).length}</div><div class="stat-label">Features Enabled</div></div>
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
        <div class="card">
          <h3 style="margin-bottom:12px">Supported Features</h3>
          ${Object.entries(features).map(([k,v]) => `
            <div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6">
              <span style="text-transform:capitalize">${k.replace(/_/g, ' ')}</span>
              <span style="color:${v ? 'var(--primary)' : '#999'};font-weight:600">${v ? 'Enabled' : 'Disabled'}</span>
            </div>
          `).join('')}
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px">Scoring Categories</h3>
          ${(config.categories || []).map(c => `
            <div style="padding:6px 0;border-bottom:1px solid #f3f4f6">
              <span style="font-weight:500">${(config.category_labels || {})[c] || c}</span>
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:12px">Custom Rubrics</h3>
        ${rubrics.length === 0 ? '<p style="color:#999">No custom rubrics yet. Create one to define custom scoring criteria.</p>' : ''}
        <div style="display:grid;gap:12px">
          ${rubrics.map(r => `
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:16px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h4 style="margin:0">${r.name} ${r.is_default ? '<span style="background:var(--primary);color:#000;padding:2px 8px;border-radius:4px;font-size:11px">DEFAULT</span>' : ''}</h4>
                <button class="btn btn-sm btn-outline" style="color:#dc2626;border-color:#dc2626" onclick="deleteRubric('${r.id}')">Delete</button>
              </div>
              <p style="margin:0;color:#666;font-size:13px">${r.description || 'No description'}</p>
              <div style="margin-top:8px;font-size:12px;color:#999">
                ${Array.isArray(r.criteria) ? r.criteria.length + ' criteria' : ''} &middot; Scale: ${r.scoring_scale || '0-100'}
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <div id="rubric-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;display:none;align-items:center;justify-content:center">
        <div style="background:#fff;border-radius:12px;padding:24px;max-width:500px;width:90%">
          <h3>Create Scoring Rubric</h3>
          <input id="rubric-name" class="form-input" placeholder="Rubric name" style="margin-bottom:8px">
          <textarea id="rubric-desc" class="form-input" placeholder="Description" rows="2" style="margin-bottom:8px"></textarea>
          <textarea id="rubric-criteria" class="form-input" placeholder='Criteria (JSON array, e.g. [{"name":"Communication","weight":0.3},{"name":"Knowledge","weight":0.7}])' rows="3" style="margin-bottom:12px"></textarea>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-outline" onclick="document.getElementById('rubric-modal').style.display='none'">Cancel</button>
            <button class="btn btn-primary" onclick="createRubric()">Create</button>
          </div>
        </div>
      </div>
    `;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${e.message}</p></div>`; }
}

function showCreateRubricModal() {
  document.getElementById('rubric-modal').style.display = 'flex';
}

async function createRubric() {
  const name = document.getElementById('rubric-name').value;
  let criteria;
  try { criteria = JSON.parse(document.getElementById('rubric-criteria').value); }
  catch { showToast('Invalid JSON for criteria', 'error'); return; }
  const res = await api('POST', '/api/scoring/rubrics', {
    name, description: document.getElementById('rubric-desc').value, criteria
  });
  if (res.rubric) { showToast('Rubric created!'); document.getElementById('rubric-modal').style.display = 'none'; renderAiScoring(); }
  else showToast(res.error || 'Failed', 'error');
}

async function deleteRubric(id) {
  if (!confirm('Delete this rubric?')) return;
  const res = await api('DELETE', `/api/scoring/rubrics/${id}`);
  if (res.success) { showToast('Rubric deleted'); renderAiScoring(); }
}


// ==================== CYCLE 16: INTEGRATIONS HUB ====================
async function renderIntegrationsHub() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';

  try {
    const [hooksRes, widgetsRes] = await Promise.all([
      api('GET', '/api/webhooks/v2'),
      api('GET', '/api/embed/widgets')
    ]);

    const hooks = hooksRes.webhooks || [];
    const widgets = widgetsRes.widgets || [];

    content.innerHTML = `
      <div class="page-header"><h2>Embed &amp; Connect</h2></div>

      <div class="card" style="margin-bottom:24px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h3>Webhooks (v2)</h3>
          <button class="btn btn-outline" onclick="simulateWebhook()">Simulate Event</button>
        </div>
        ${hooks.length === 0 ? '<p style="color:#999">No webhooks configured. Add webhooks from the Integrations page.</p>' : ''}
        <div style="display:grid;gap:12px">
          ${hooks.map(h => `
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                  <div style="font-weight:600;font-size:14px">${h.url}</div>
                  <div style="font-size:12px;color:#666;margin-top:2px">Events: ${h.events || 'all'}</div>
                </div>
                <div style="display:flex;gap:8px;align-items:center">
                  <span style="font-size:11px;padding:2px 8px;border-radius:4px;background:${h.active ? '#e6fce6' : '#fee2e2'};color:${h.active ? '#166534' : '#dc2626'}">${h.active ? 'Active' : 'Inactive'}</span>
                  <button class="btn btn-sm btn-outline" onclick="viewDeliveries('${h.id}')">Deliveries</button>
                </div>
              </div>
              ${h.delivery_stats ? `<div style="font-size:12px;color:#999;margin-top:6px">Delivered: ${h.delivery_stats.delivered||0}/${h.delivery_stats.total||0}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>

      <div class="card" style="margin-bottom:24px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h3>Embeddable Widgets</h3>
          <button class="btn btn-primary" onclick="showCreateWidgetModal()">Create Widget</button>
        </div>
        ${widgets.length === 0 ? '<p style="color:#999">No widgets yet. Create one to embed an apply form on your website.</p>' : ''}
        <div style="display:grid;gap:12px">
          ${widgets.map(w => `
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:14px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                  <div style="font-weight:600">${w.title || w.widget_type || 'Widget'}</div>
                  <div style="font-size:12px;color:#666">Key: ${w.embed_key} &middot; Views: ${w.views||0} &middot; Submissions: ${w.submissions||0}</div>
                </div>
                <span style="font-size:11px;padding:2px 8px;border-radius:4px;background:${w.active ? '#e6fce6' : '#fee2e2'}">${w.active ? 'Active' : 'Inactive'}</span>
              </div>
              <div style="margin-top:8px;background:#f9fafb;padding:8px;border-radius:4px;font-family:monospace;font-size:12px">
                &lt;script src="/embed/${w.embed_key}.js"&gt;&lt;/script&gt;
              </div>
            </div>
          `).join('')}
        </div>
      </div>

      <div id="deliveries-panel" style="display:none" class="card">
        <h3 style="margin-bottom:12px">Delivery Log</h3>
        <div id="deliveries-list"></div>
      </div>

      <div id="widget-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
        <div style="background:#fff;border-radius:12px;padding:24px;max-width:400px;width:90%">
          <h3>Create Widget</h3>
          <select id="widget-interview" class="form-input" style="margin-bottom:8px"><option value="">Select interview...</option></select>
          <input id="widget-title" class="form-input" placeholder="Widget title" style="margin-bottom:12px">
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-outline" onclick="document.getElementById('widget-modal').style.display='none'">Cancel</button>
            <button class="btn btn-primary" onclick="createWidget()">Create</button>
          </div>
        </div>
      </div>
    `;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${e.message}</p></div>`; }
}

async function simulateWebhook() {
  const res = await api('POST', '/api/webhooks/v2/simulate', {event_type: 'candidate.completed'});
  if (res.simulated) showToast('Simulated event: ' + res.event_type);
}

async function viewDeliveries(hookId) {
  const res = await api('GET', `/api/webhooks/v2/deliveries/${hookId}`);
  const panel = document.getElementById('deliveries-panel');
  panel.style.display = 'block';
  const list = document.getElementById('deliveries-list');
  const deliveries = res.deliveries || [];
  list.innerHTML = deliveries.length === 0 ? '<p style="color:#999">No deliveries yet</p>' :
    deliveries.map(d => `<div style="padding:8px 0;border-bottom:1px solid #f3f4f6;font-size:13px">
      <span style="font-weight:500">${d.event_type}</span> &middot;
      Status: ${d.response_status||'pending'} &middot; ${d.created_at}
    </div>`).join('');
}

async function showCreateWidgetModal() {
  document.getElementById('widget-modal').style.display = 'flex';
  const interviews = await api('GET', '/api/interviews');
  const list = Array.isArray(interviews) ? interviews : (interviews.interviews || []);
  const sel = document.getElementById('widget-interview');
  sel.innerHTML = '<option value="">Select interview...</option>' + list.map(i => `<option value="${i.id}">${i.title}</option>`).join('');
}

async function createWidget() {
  const interview_id = document.getElementById('widget-interview').value;
  if (!interview_id) { showToast('Select an interview', 'error'); return; }
  const res = await api('POST', '/api/embed/widgets', {
    interview_id, title: document.getElementById('widget-title').value, widget_type: 'apply_button'
  });
  if (res.widget) { showToast('Widget created!'); document.getElementById('widget-modal').style.display = 'none'; renderIntegrationsHub(); }
  else showToast(res.error || 'Failed', 'error');
}


// ==================== CYCLE 17: REVIEW & COMPARISON ====================
async function renderReviewHub() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';
  try {
    const [slRes, candsRes] = await Promise.all([
      api('GET', '/api/shortlists'),
      api('GET', '/api/candidates')
    ]);
    const shortlists = slRes.shortlists || [];
    const candidates = (candsRes.candidates || []).slice(0, 20);

    content.innerHTML = `
      <div class="page-header"><h2>Compare Candidates</h2>
        <button class="btn btn-primary" onclick="showCreateShortlistModal()">New Shortlist</button>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
        <div class="card">
          <h3 style="margin-bottom:12px">Shortlists</h3>
          ${shortlists.length === 0 ? '<p style="color:#999">No shortlists yet.</p>' : ''}
          ${shortlists.map(s => `
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:8px;cursor:pointer" onclick="viewShortlist('${s.id}')">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <strong>${s.name}</strong>
                <span style="font-size:12px;color:#666">${s.candidate_count || 0} candidates</span>
              </div>
              ${s.description ? `<div style="font-size:13px;color:#666;margin-top:4px">${s.description}</div>` : ''}
            </div>
          `).join('')}
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px">Quick Compare</h3>
          <p style="font-size:13px;color:#666;margin-bottom:12px">Select 2+ candidates to compare side-by-side:</p>
          <div style="max-height:300px;overflow-y:auto">
            ${candidates.map(c => `
              <label style="display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #f3f4f6;cursor:pointer">
                <input type="checkbox" class="compare-check" value="${c.id}">
                <span>${c.first_name} ${c.last_name}</span>
                <span style="margin-left:auto;font-size:12px;color:#666">${c.status} ${c.ai_score ? '· Score: '+c.ai_score : ''}</span>
              </label>
            `).join('')}
          </div>
          <button class="btn btn-outline" style="margin-top:12px" onclick="compareCandidates()">Compare Selected</button>
        </div>
      </div>
      <div id="compare-results" class="card" style="display:none"></div>
      <div id="shortlist-detail" class="card" style="display:none"></div>
      <div id="shortlist-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
        <div style="background:#fff;border-radius:12px;padding:24px;max-width:400px;width:90%">
          <h3>Create Shortlist</h3>
          <input id="sl-name" class="form-input" placeholder="Shortlist name" style="margin-bottom:8px">
          <input id="sl-desc" class="form-input" placeholder="Description (optional)" style="margin-bottom:12px">
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-outline" onclick="document.getElementById('shortlist-modal').style.display='none'">Cancel</button>
            <button class="btn btn-primary" onclick="createShortlist()">Create</button>
          </div>
        </div>
      </div>`;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${e.message}</p></div>`; }
}

function showCreateShortlistModal() { document.getElementById('shortlist-modal').style.display = 'flex'; }

async function createShortlist() {
  const res = await api('POST', '/api/shortlists', {
    name: document.getElementById('sl-name').value,
    description: document.getElementById('sl-desc').value
  });
  if (res.shortlist) { showToast('Shortlist created'); document.getElementById('shortlist-modal').style.display = 'none'; renderReviewHub(); }
  else showToast(res.error || 'Failed', 'error');
}

async function viewShortlist(id) {
  const res = await api('GET', `/api/shortlists/${id}`);
  const panel = document.getElementById('shortlist-detail');
  panel.style.display = 'block';
  const cands = res.candidates || [];
  panel.innerHTML = `<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
    <h3>${res.shortlist?.name || 'Shortlist'}</h3>
    <button class="btn btn-sm btn-outline" style="color:#dc2626" onclick="deleteShortlist('${id}')">Delete</button>
  </div>
  ${cands.length === 0 ? '<p style="color:#999">No candidates in this shortlist</p>' : ''}
  ${cands.map((c,i) => `<div style="display:flex;justify-content:space-between;padding:8px 0;border-bottom:1px solid #f3f4f6">
    <span>#${i+1} ${c.first_name} ${c.last_name} (${c.email})</span>
    <span>Score: ${c.ai_score || '-'} | ${c.status}</span>
  </div>`).join('')}`;
}

async function deleteShortlist(id) {
  if (!confirm('Delete this shortlist?')) return;
  await api('DELETE', `/api/shortlists/${id}`);
  showToast('Deleted'); renderReviewHub();
}

async function compareCandidates() {
  const ids = [...document.querySelectorAll('.compare-check:checked')].map(c => c.value);
  if (ids.length < 2) { showToast('Select at least 2 candidates', 'error'); return; }
  const res = await api('POST', '/api/candidates/compare', { candidate_ids: ids });
  const panel = document.getElementById('compare-results');
  panel.style.display = 'block';
  const cands = res.candidates || [];
  panel.innerHTML = `<h3 style="margin-bottom:12px">Comparison (${cands.length} candidates)</h3>
    <table class="data-table"><thead><tr><th>Name</th><th>Status</th><th>AI Score</th><th>Responses</th><th>Notes</th></tr></thead>
    <tbody>${cands.map(c => `<tr><td>${c.first_name} ${c.last_name}</td><td>${c.status}</td>
      <td>${c.ai_score || '-'}</td><td>${c.response_count}</td><td>${c.notes_count}</td></tr>`).join('')}
    </tbody></table>`;
}


// ==================== CYCLE 29: FMO ADMIN PORTAL ====================
async function renderFmoPortal() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';
  try {
    const [agenciesRes, statsRes] = await Promise.all([
      api('GET', '/api/fmo/agencies'),
      api('GET', '/api/fmo/stats')
    ]);
    const agencies = agenciesRes.agencies || [];
    const stats = statsRes || {};

    const planColors = {
      'free': '#94a3b8', 'essentials': '#0ace0a', 'professional': '#2563eb',
      'enterprise': '#7c3aed', 'starter': '#0ace0a', 'pro': '#2563eb'
    };
    const planLabels = {
      'free': 'Free', 'starter': 'Starter $99', 'professional': 'Professional $179',
      'enterprise': 'Enterprise $299', 'essentials': 'Starter $99', 'pro': 'Professional $179'
    };

    content.innerHTML = `
      <div class="page-header">
        <div>
          <h2 style="margin:0">FMO Admin Portal</h2>
          <p style="color:#666;font-size:14px;margin:4px 0 0">Manage your agencies, monitor usage, and control subscription tiers</p>
        </div>
        <button class="btn btn-primary" onclick="showAddAgencyModal()">+ Add Agency</button>
      </div>

      <div class="stats-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
        <div class="stat-card" style="border-left:4px solid #0ace0a">
          <div class="stat-value">${stats.total_agencies||0}</div><div class="stat-label">Total Agencies</div>
        </div>
        <div class="stat-card" style="border-left:4px solid #2563eb">
          <div class="stat-value">${stats.total_candidates||0}</div><div class="stat-label">Total Candidates</div>
        </div>
        <div class="stat-card" style="border-left:4px solid #7c3aed">
          <div class="stat-value">${stats.total_interviews||0}</div><div class="stat-label">Total Interviews</div>
        </div>
        <div class="stat-card" style="border-left:4px solid #f59e0b">
          <div class="stat-value">${stats.active_this_month||0}</div><div class="stat-label">Active This Month</div>
        </div>
      </div>

      ${Object.keys(stats.plan_distribution||{}).length > 0 ? `
      <div class="card" style="margin-bottom:20px;padding:16px">
        <h3 style="margin:0 0 12px;font-size:15px">Plan Distribution</h3>
        <div style="display:flex;gap:16px;flex-wrap:wrap">
          ${Object.entries(stats.plan_distribution||{}).map(([plan, count]) => `
            <div style="display:flex;align-items:center;gap:8px">
              <span style="width:12px;height:12px;border-radius:50%;background:${planColors[plan]||'#94a3b8'}"></span>
              <span style="font-size:14px"><strong>${count}</strong> ${planLabels[plan]||plan}</span>
            </div>
          `).join('')}
        </div>
      </div>` : ''}

      <div class="card">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h3 style="margin:0">Managed Agencies (${agencies.length})</h3>
          <input id="agency-search" class="form-input" placeholder="Search agencies..." style="width:250px;font-size:13px" oninput="filterAgencies()">
        </div>
        ${agencies.length === 0 ? '<p style="color:#999;text-align:center;padding:30px 0">No agencies yet. Click "+ Add Agency" to onboard your first agency.</p>' : `
        <div id="agencies-list" style="display:grid;gap:12px">
          ${agencies.map(a => {
            const plan = a.plan || 'free';
            const subStatus = a.subscription_status || 'none';
            const statusColor = subStatus === 'active' || subStatus === 'trialing' ? '#e6fce6' : subStatus === 'none' ? '#f3f4f6' : '#fee2e2';
            const statusLabel = subStatus === 'trialing' ? 'Trial' : subStatus === 'active' ? 'Active' : subStatus === 'canceled' ? 'Canceled' : 'No Sub';
            return `
            <div class="agency-row" data-name="${(a.agency_name||'').toLowerCase()}" style="border:1px solid #e5e7eb;border-radius:8px;padding:16px;transition:box-shadow .15s" onmouseover="this.style.boxShadow='0 2px 8px rgba(0,0,0,.08)'" onmouseout="this.style.boxShadow='none'">
              <div style="display:flex;justify-content:space-between;align-items:flex-start">
                <div style="flex:1">
                  <div style="display:flex;align-items:center;gap:8px">
                    <span style="font-weight:600;font-size:15px">${a.agency_name||'Unnamed Agency'}</span>
                    ${a.is_self ? '<span style="font-size:10px;padding:2px 6px;border-radius:3px;background:#0ace0a;color:#000;font-weight:600">YOU</span>' : ''}
                  </div>
                  <div style="font-size:13px;color:#666;margin-top:4px">${a.name||''} &middot; ${a.email||''}</div>
                  <div style="display:flex;gap:16px;margin-top:10px;font-size:12px;color:#888">
                    <span>${a.total_interviews||0} interviews</span>
                    <span>${a.total_candidates||0} candidates</span>
                    <span>${(a.team_size||0)+1} team members</span>
                    <span>Joined ${a.created_at ? new Date(a.created_at).toLocaleDateString() : 'N/A'}</span>
                  </div>
                </div>
                <div style="display:flex;align-items:center;gap:10px">
                  <span style="font-size:11px;padding:3px 8px;border-radius:4px;background:${statusColor};font-weight:500">${statusLabel}</span>
                  <select onchange="changePlan('${a.id}', this.value)" style="font-size:12px;padding:4px 8px;border:1px solid #d1d5db;border-radius:4px;cursor:pointer"${a.is_self ? ' disabled' : ''}>
                    <option value="free" ${plan==='free'?'selected':''}>Free</option>
                    <option value="starter" ${plan==='starter'||plan==='essentials'?'selected':''}>Starter $99</option>
                    <option value="professional" ${plan==='professional'||plan==='pro'?'selected':''}>Professional $179</option>
                    <option value="enterprise" ${plan==='enterprise'?'selected':''}>Enterprise $299</option>
                  </select>
                  ${!a.is_self ? `<button onclick="deleteAgency('${a.id}','${(a.agency_name||'').replace(/'/g,"\\'")}')" style="font-size:11px;padding:4px 8px;border:1px solid #e5e7eb;border-radius:4px;background:#fff;color:#dc2626;cursor:pointer" onmouseover="this.style.background='#fee2e2'" onmouseout="this.style.background='#fff'">Delete</button>` : ''}
                </div>
              </div>
            </div>`;
          }).join('')}
        </div>`}
      </div>

      <div id="agency-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
        <div style="background:#fff;border-radius:12px;padding:28px;max-width:440px;width:90%">
          <h3 style="margin:0 0 16px">Add New Agency</h3>
          <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Contact Name</label>
          <input id="agency-contact-name" class="form-input" placeholder="e.g. John Smith" style="margin-bottom:12px">
          <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Email</label>
          <input id="agency-email" class="form-input" placeholder="e.g. john@agencyname.com" style="margin-bottom:12px">
          <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Agency Name</label>
          <input id="agency-name-input" class="form-input" placeholder="e.g. Smith Insurance Group" style="margin-bottom:12px">
          <label style="font-size:13px;font-weight:500;display:block;margin-bottom:4px">Starting Plan</label>
          <select id="agency-plan" class="form-input" style="margin-bottom:16px">
            <option value="professional" selected>Professional $179/mo (30-day free trial)</option>
            <option value="starter">Starter $99/mo</option>
            <option value="enterprise">Enterprise $299/mo</option>
            <option value="free">Free</option>
          </select>
          <p style="font-size:12px;color:#888;margin:0 0 16px">The agency will receive a 30-day Professional trial. A temporary password will be generated.</p>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-outline" onclick="document.getElementById('agency-modal').style.display='none'">Cancel</button>
            <button class="btn btn-primary" onclick="addAgency()">Create Agency</button>
          </div>
        </div>
      </div>`;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error loading FMO Portal</h3><p>${e.message}</p></div>`; }
}

function showAddAgencyModal() { document.getElementById('agency-modal').style.display = 'flex'; }

function filterAgencies() {
  const q = (document.getElementById('agency-search').value || '').toLowerCase();
  document.querySelectorAll('.agency-row').forEach(row => {
    row.style.display = row.dataset.name.includes(q) ? '' : 'none';
  });
}

async function addAgency() {
  const name = document.getElementById('agency-contact-name').value.trim();
  const email = document.getElementById('agency-email').value.trim();
  const agencyName = document.getElementById('agency-name-input').value.trim();
  const plan = document.getElementById('agency-plan').value;
  if (!name || !email || !agencyName) { toast('Please fill in all fields', 'error'); return; }
  const res = await api('POST', '/api/fmo/agencies', { name, email, agency_name: agencyName, plan });
  if (res.success) {
    document.getElementById('agency-modal').innerHTML = `
      <div style="background:#fff;border-radius:12px;padding:32px;max-width:480px;width:90%;text-align:center">
        <div style="background:#0ace0a;color:#000;width:48px;height:48px;border-radius:50%;display:flex;align-items:center;justify-content:center;margin:0 auto 16px;font-size:24px">✓</div>
        <h3 style="margin:0 0 8px">Agency Created!</h3>
        <p style="color:#555;margin:0 0 20px">${res.agency.agency_name} is ready to go.</p>
        <div style="background:#f3f4f6;border-radius:8px;padding:16px;text-align:left;margin:0 0 20px">
          <p style="margin:0 0 6px"><strong>Email:</strong> ${res.agency.email}</p>
          <p style="margin:0 0 6px"><strong>Temp Password:</strong> <code style="background:#e8e8e8;padding:2px 8px;border-radius:4px;font-size:15px;user-select:all">${res.agency.temp_password}</code></p>
          <p style="margin:0"><strong>Plan:</strong> ${res.agency.plan.charAt(0).toUpperCase() + res.agency.plan.slice(1)}</p>
        </div>
        <p style="color:#888;font-size:13px;margin:0 0 20px">Share these credentials with the agency. They'll be prompted to change the password on first login.</p>
        <button class="btn btn-primary" onclick="document.getElementById('agency-modal').style.display='none';renderFmoPortal()">Done</button>
      </div>`;
  } else toast(res.error || 'Failed to create agency', 'error');
}

async function changePlan(agencyId, newPlan) {
  const res = await api('PUT', `/api/fmo/agencies/${agencyId}/plan`, { plan: newPlan });
  if (res.success) toast(`Plan updated to ${newPlan}`);
  else { toast(res.error || 'Failed', 'error'); renderFmoPortal(); }
}

async function deleteAgency(agencyId, agencyName) {
  if (!confirm(`Delete "${agencyName}" and all its data? This cannot be undone.`)) return;
  try {
    const res = await api('DELETE', `/api/fmo/agencies/${agencyId}`);
    if (res.success) { toast(`Deleted ${res.deleted_email}`, 'success'); renderFmoPortal(); }
    else toast(res.error || 'Failed to delete', 'error');
  } catch(e) { toast(e.message || 'Failed to delete', 'error'); }
}


// ==================== CYCLE 17: EMAIL TEMPLATES ====================
async function renderEmailTemplates() {
  const content = document.getElementById('page-content');
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';
  try {
    const [tmplRes, statsRes] = await Promise.all([
      api('GET', '/api/email/templates'),
      api('GET', '/api/email/send-stats')
    ]);
    const templates = tmplRes.templates || [];
    const defaultTypes = tmplRes.default_types || {};
    const stats = statsRes;

    content.innerHTML = `
      <div class="page-header"><h2>Email Templates</h2>
        <button class="btn btn-primary" onclick="showCreateTemplateModal()">New Template</button>
      </div>
      <div class="stats-grid" style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
        <div class="stat-card"><div class="stat-value">${stats.total||0}</div><div class="stat-label">Total Emails</div></div>
        <div class="stat-card"><div class="stat-value">${stats.sent||0}</div><div class="stat-label">Delivered</div></div>
        <div class="stat-card"><div class="stat-value">${stats.failed||0}</div><div class="stat-label">Failed</div></div>
        <div class="stat-card"><div class="stat-value">${stats.delivery_rate||0}%</div><div class="stat-label">Delivery Rate</div></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-bottom:24px">
        <div class="card">
          <h3 style="margin-bottom:12px">Custom Templates</h3>
          ${templates.length === 0 ? '<p style="color:#999">No custom templates. Using system defaults.</p>' : ''}
          ${templates.map(t => `
            <div style="border:1px solid #e5e7eb;border-radius:8px;padding:12px;margin-bottom:8px">
              <div style="display:flex;justify-content:space-between;align-items:center">
                <div>
                  <strong>${t.name}</strong> ${t.is_default ? '<span style="background:var(--primary);color:#000;padding:1px 6px;border-radius:3px;font-size:10px">DEFAULT</span>' : ''}
                  <div style="font-size:12px;color:#666">${t.template_type} &middot; Subject: ${t.subject}</div>
                </div>
                <button class="btn btn-sm btn-outline" style="color:#dc2626" onclick="deleteTemplate('${t.id}')">Delete</button>
              </div>
            </div>
          `).join('')}
        </div>
        <div class="card">
          <h3 style="margin-bottom:12px">Available Template Types</h3>
          ${Object.entries(defaultTypes).map(([k,v]) => `
            <div style="padding:8px 0;border-bottom:1px solid #f3f4f6">
              <div style="font-weight:500;text-transform:capitalize">${k.replace(/_/g, ' ')}</div>
              <div style="font-size:12px;color:#666">Variables: ${(v.variables||[]).join(', ')}</div>
            </div>
          `).join('')}
        </div>
      </div>
      <div class="card">
        <h3 style="margin-bottom:12px">Recent Emails</h3>
        ${(stats.recent||[]).length === 0 ? '<p style="color:#999">No emails sent yet.</p>' : ''}
        ${(stats.recent||[]).slice(0,10).map(e => `
          <div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6;font-size:13px">
            <span>${e.to_email} &middot; ${e.email_type||'unknown'}</span>
            <span style="color:${e.status==='sent' ? 'var(--primary)' : '#dc2626'}">${e.status} &middot; ${e.sent_at||''}</span>
          </div>
        `).join('')}
      </div>
      <div id="template-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1001;align-items:center;justify-content:center">
        <div style="background:#fff;border-radius:12px;padding:24px;max-width:500px;width:90%">
          <h3>Create Email Template</h3>
          <select id="tmpl-type" class="form-input" style="margin-bottom:8px">
            ${Object.keys(defaultTypes).map(k => `<option value="${k}">${k.replace(/_/g,' ')}</option>`).join('')}
          </select>
          <input id="tmpl-name" class="form-input" placeholder="Template name" style="margin-bottom:8px">
          <input id="tmpl-subject" class="form-input" placeholder="Subject line (use {{variable}})" style="margin-bottom:8px">
          <textarea id="tmpl-body" class="form-input" placeholder="HTML body (use {{variable}} for dynamic content)" rows="5" style="margin-bottom:8px"></textarea>
          <button class="btn btn-outline btn-sm" onclick="previewTemplate()" style="margin-bottom:12px">Preview</button>
          <div id="tmpl-preview" style="display:none;background:#f9fafb;padding:12px;border-radius:8px;margin-bottom:12px;font-size:13px"></div>
          <div style="display:flex;gap:8px;justify-content:flex-end">
            <button class="btn btn-outline" onclick="document.getElementById('template-modal').style.display='none'">Cancel</button>
            <button class="btn btn-primary" onclick="createTemplate()">Create</button>
          </div>
        </div>
      </div>`;
  } catch(e) { content.innerHTML = `<div class="empty-state"><h3>Error</h3><p>${e.message}</p></div>`; }
}

function showCreateTemplateModal() { document.getElementById('template-modal').style.display = 'flex'; }

async function previewTemplate() {
  const res = await api('POST', '/api/email/preview', {
    subject: document.getElementById('tmpl-subject').value,
    html_body: document.getElementById('tmpl-body').value,
    template_type: document.getElementById('tmpl-type').value
  });
  const preview = document.getElementById('tmpl-preview');
  preview.style.display = 'block';
  preview.innerHTML = `<strong>Subject:</strong> ${res.subject}<hr style="margin:8px 0"><div>${res.html_body || 'Empty body'}</div>`;
}

async function createTemplate() {
  const res = await api('POST', '/api/email/templates', {
    template_type: document.getElementById('tmpl-type').value,
    name: document.getElementById('tmpl-name').value,
    subject: document.getElementById('tmpl-subject').value,
    html_body: document.getElementById('tmpl-body').value,
  });
  if (res.template) { showToast('Template created!'); document.getElementById('template-modal').style.display = 'none'; renderEmailTemplates(); }
  else showToast(res.error || 'Failed', 'error');
}

async function deleteTemplate(id) {
  if (!confirm('Delete this template?')) return;
  await api('DELETE', `/api/email/templates/${id}`);
  showToast('Deleted'); renderEmailTemplates();
}


// ==================== CYCLE 18: WHITE-LABEL & BRANDING ====================

async function renderWhiteLabel() {
  const [profilesRes, previewRes] = await Promise.all([
    api('GET', '/api/branding/profiles'),
    api('POST', '/api/branding/preview', {}),
  ]);
  const profiles = profilesRes.profiles || [];
  const preview = previewRes.preview || {};

  content.innerHTML = `
    <div class="page-header"><h1>Custom Look &amp; Feel</h1>
      <button class="btn btn-primary" onclick="showCreateBrandModal()">+ New Brand Profile</button></div>
    <div class="stats-grid" style="grid-template-columns:repeat(3,1fr);margin-bottom:24px">
      <div class="stat-card"><div class="stat-value">${profiles.length}</div><div class="stat-label">Brand Profiles</div></div>
      <div class="stat-card"><div class="stat-value">${profiles.filter(p=>p.is_active).length}</div><div class="stat-label">Active</div></div>
      <div class="stat-card"><div class="stat-value" style="font-size:14px">${preview.company_name||'Not Set'}</div><div class="stat-label">Current Brand</div></div>
    </div>
    <div class="card"><div class="card-header"><h3>Brand Profiles</h3></div><div class="card-body">
      ${profiles.length===0?'<p style="color:#999">No brand profiles yet. Create one to customize the candidate experience.</p>':
      '<table class="data-table"><thead><tr><th>Name</th><th>Colors</th><th>Company</th><th>Status</th><th>Actions</th></tr></thead><tbody>'+
      profiles.map(p=>`<tr><td><strong>${p.profile_name}</strong></td>
        <td><span style="display:inline-block;width:20px;height:20px;border-radius:4px;background:${p.primary_color};vertical-align:middle"></span>
        <span style="display:inline-block;width:20px;height:20px;border-radius:4px;background:${p.secondary_color};vertical-align:middle;margin-left:4px"></span></td>
        <td>${p.company_name||'-'}</td>
        <td><span class="badge ${p.is_active?'badge-success':'badge-secondary'}">${p.is_active?'Active':'Inactive'}</span></td>
        <td><button class="btn btn-sm btn-outline" onclick="activateBrandProfile('${p.id}')">Activate</button>
        <button class="btn btn-sm btn-danger" onclick="deleteBrandProfile('${p.id}')">Delete</button></td></tr>`).join('')+
      '</tbody></table>'}
    </div></div>
    <div id="brand-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;display:none;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:12px;padding:24px;width:480px;max-height:80vh;overflow-y:auto">
        <h3>Create Brand Profile</h3>
        <div style="margin:12px 0"><label>Profile Name</label><input id="bp-name" class="form-input" placeholder="e.g. My Agency Brand"></div>
        <div style="margin:12px 0"><label>Company Name</label><input id="bp-company" class="form-input" placeholder="Your company name"></div>
        <div style="margin:12px 0"><label>Tagline</label><input id="bp-tagline" class="form-input" placeholder="Your tagline"></div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:12px 0">
          <div><label>Primary Color</label><input id="bp-primary" type="color" value="#0ace0a" style="width:100%;height:40px"></div>
          <div><label>Secondary Color</label><input id="bp-secondary" type="color" value="#000000" style="width:100%;height:40px"></div>
        </div>
        <div style="margin:12px 0"><label>Welcome Message</label><textarea id="bp-welcome" class="form-input" rows="2" placeholder="Welcome message for candidates"></textarea></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-outline" onclick="document.getElementById('brand-modal').style.display='none'">Cancel</button>
          <button class="btn btn-primary" onclick="createBrandProfile()">Create</button></div>
      </div></div>`;
}

function showCreateBrandModal() {
  const m = document.getElementById('brand-modal');
  m.style.display = 'flex';
}

async function createBrandProfile() {
  const res = await api('POST', '/api/branding/profiles', {
    profile_name: document.getElementById('bp-name').value,
    company_name: document.getElementById('bp-company').value,
    tagline: document.getElementById('bp-tagline').value,
    primary_color: document.getElementById('bp-primary').value,
    secondary_color: document.getElementById('bp-secondary').value,
    welcome_message: document.getElementById('bp-welcome').value,
  });
  if (res.profile) { showToast('Brand profile created!'); document.getElementById('brand-modal').style.display='none'; renderWhiteLabel(); }
  else showToast(res.error||'Failed','error');
}

async function activateBrandProfile(id) {
  const res = await api('POST', `/api/branding/profiles/${id}/activate`);
  if (res.success) { showToast('Profile activated!'); renderWhiteLabel(); }
  else showToast(res.error||'Failed','error');
}

async function deleteBrandProfile(id) {
  if (!confirm('Delete this brand profile?')) return;
  await api('DELETE', `/api/branding/profiles/${id}`);
  showToast('Deleted'); renderWhiteLabel();
}


// ==================== CYCLE 18: REPORT GENERATION & SHARING ====================

async function renderReportHub() {
  const res = await api('GET', '/api/reports');
  const reports = res.reports || [];

  content.innerHTML = `
    <div class="page-header"><h1>All Reports</h1></div>
    <div class="stats-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:24px">
      <div class="stat-card"><div class="stat-value">${reports.length}</div><div class="stat-label">Total Reports</div></div>
      <div class="stat-card"><div class="stat-value">${reports.filter(r=>r.recommendation==='Strong Hire').length}</div><div class="stat-label">Strong Hires</div></div>
      <div class="stat-card"><div class="stat-value">${reports.filter(r=>r.recommendation==='Hire').length}</div><div class="stat-label">Hires</div></div>
      <div class="stat-card"><div class="stat-value">${reports.filter(r=>r.shared_with).length}</div><div class="stat-label">Shared</div></div>
    </div>
    <div class="card"><div class="card-header"><h3>Generated Reports</h3>
      <p style="color:#666;font-size:13px">Generate scorecards from the Candidates page, then manage & share them here.</p></div>
    <div class="card-body">
      ${reports.length===0?'<p style="color:#999">No reports generated yet. Go to Candidates and generate a scorecard.</p>':
      '<table class="data-table"><thead><tr><th>Candidate</th><th>Interview</th><th>Score</th><th>Recommendation</th><th>Shared</th><th>Actions</th></tr></thead><tbody>'+
      reports.map(r=>`<tr>
        <td><strong>${r.candidate_name||'-'}</strong></td>
        <td>${r.title||'-'}</td>
        <td>${(r.scores_json||[]).length>0?((r.scores_json.reduce((a,s)=>a+parseFloat(s.score||0),0)/r.scores_json.length).toFixed(1)+'/10'):'-'}</td>
        <td><span class="badge ${r.recommendation==='Strong Hire'?'badge-success':r.recommendation==='Hire'?'badge-info':r.recommendation==='No Hire'?'badge-danger':'badge-warning'}">${r.recommendation||'-'}</span></td>
        <td>${r.shared_with?'Yes':'No'}</td>
        <td><button class="btn btn-sm btn-outline" onclick="shareReport('${r.id}')">Share</button>
        <button class="btn btn-sm btn-danger" onclick="deleteReport('${r.id}')">Delete</button></td></tr>`).join('')+
      '</tbody></table>'}
    </div></div>`;
}

async function shareReport(id) {
  const email = prompt('Enter email to share report with:');
  if (!email) return;
  const res = await api('POST', `/api/reports/${id}/share`, { email });
  if (res.success) showToast(`Report shared! Link: ${res.share_url}`);
  else showToast(res.error||'Failed','error');
}

async function deleteReport(id) {
  if (!confirm('Delete this report?')) return;
  await api('DELETE', `/api/reports/${id}`);
  showToast('Deleted'); renderReportHub();
}


// ==================== CYCLE 18: BULK OPERATIONS & WORKFLOWS ====================

async function renderBulkOps() {
  const [opsRes, wfRes, intRes] = await Promise.all([
    api('GET', '/api/bulk/operations'),
    api('GET', '/api/workflows'),
    api('GET', '/api/interviews'),
  ]);
  const ops = opsRes.operations || [];
  const workflows = wfRes.workflows || [];
  const interviews = (Array.isArray(intRes) ? intRes : intRes.interviews || []);

  content.innerHTML = `
    <div class="page-header"><h1>Bulk Actions</h1></div>
    <div class="stats-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:24px">
      <div class="stat-card"><div class="stat-value">${ops.length}</div><div class="stat-label">Operations Run</div></div>
      <div class="stat-card"><div class="stat-value">${ops.reduce((a,o)=>a+(o.processed_items||0),0)}</div><div class="stat-label">Items Processed</div></div>
      <div class="stat-card"><div class="stat-value">${workflows.length}</div><div class="stat-label">Workflow Rules</div></div>
      <div class="stat-card"><div class="stat-value">${workflows.filter(w=>w.is_enabled).length}</div><div class="stat-label">Active Rules</div></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
      <div class="card"><div class="card-header"><h3>Quick Actions</h3></div><div class="card-body">
        <div style="display:flex;flex-direction:column;gap:8px">
          <button class="btn btn-primary" onclick="showBulkInviteModal()">Bulk Invite Candidates</button>
          <button class="btn btn-outline" onclick="showBulkRemindModal()">Bulk Send Reminders</button>
        </div>
      </div></div>
      <div class="card"><div class="card-header"><h3>Workflow Automation</h3>
        <button class="btn btn-sm btn-primary" onclick="showCreateWorkflowModal()">+ New Rule</button></div>
      <div class="card-body">
        ${workflows.length===0?'<p style="color:#999">No automation rules yet.</p>':
        workflows.map(w=>`<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #eee">
          <div><strong>${w.name}</strong><br><span style="color:#666;font-size:12px">${w.trigger_type} → ${w.action_type}</span></div>
          <div><span class="badge ${w.is_enabled?'badge-success':'badge-secondary'}">${w.is_enabled?'On':'Off'}</span>
          <button class="btn btn-sm btn-danger" style="margin-left:8px" onclick="deleteWorkflow('${w.id}')">Delete</button></div>
        </div>`).join('')}
      </div></div>
    </div>
    <div class="card"><div class="card-header"><h3>Recent Operations</h3></div><div class="card-body">
      ${ops.length===0?'<p style="color:#999">No bulk operations run yet.</p>':
      '<table class="data-table"><thead><tr><th>Type</th><th>Status</th><th>Processed</th><th>Failed</th><th>Date</th></tr></thead><tbody>'+
      ops.slice(0,10).map(o=>`<tr><td>${o.operation_type}</td>
        <td><span class="badge ${o.status==='completed'?'badge-success':'badge-warning'}">${o.status}</span></td>
        <td>${o.processed_items}/${o.total_items}</td><td>${o.failed_items}</td>
        <td>${new Date(o.created_at).toLocaleDateString()}</td></tr>`).join('')+
      '</tbody></table>'}
    </div></div>
    <div id="bulk-invite-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:12px;padding:24px;width:520px">
        <h3>Bulk Invite Candidates</h3>
        <div style="margin:12px 0"><label>Interview</label><select id="bulk-interview" class="form-input">
          ${interviews.map(i=>`<option value="${i.id}">${i.title}</option>`).join('')}</select></div>
        <div style="margin:12px 0"><label>Candidates (JSON array)</label>
          <textarea id="bulk-candidates" class="form-input" rows="5" placeholder='[{"first_name":"Jane","last_name":"Smith","email":"jane@example.com"}]'></textarea></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-outline" onclick="document.getElementById('bulk-invite-modal').style.display='none'">Cancel</button>
          <button class="btn btn-primary" onclick="runBulkInvite()">Invite All</button></div>
      </div></div>
    <div id="workflow-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:12px;padding:24px;width:480px">
        <h3>Create Workflow Rule</h3>
        <div style="margin:12px 0"><label>Name</label><input id="wf-name" class="form-input" placeholder="e.g. Auto-advance high scorers"></div>
        <div style="margin:12px 0"><label>Trigger</label><select id="wf-trigger" class="form-input">
          <option value="candidate_completed">Candidate Completed</option><option value="score_threshold">Score Threshold Met</option>
          <option value="candidate_invited">Candidate Invited</option><option value="reminder_due">Reminder Due</option></select></div>
        <div style="margin:12px 0"><label>Action</label><select id="wf-action" class="form-input">
          <option value="send_email">Send Email</option><option value="advance_stage">Advance Stage</option>
          <option value="add_to_shortlist">Add to Shortlist</option><option value="notify_team">Notify Team</option>
          <option value="generate_report">Generate Report</option></select></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-outline" onclick="document.getElementById('workflow-modal').style.display='none'">Cancel</button>
          <button class="btn btn-primary" onclick="createWorkflow()">Create</button></div>
      </div></div>`;
}

function showBulkInviteModal() { document.getElementById('bulk-invite-modal').style.display='flex'; }
function showBulkRemindModal() {
  const intId = prompt('Enter Interview ID to send reminders for:');
  if (!intId) return;
  bulkRemind(intId);
}
function showCreateWorkflowModal() { document.getElementById('workflow-modal').style.display='flex'; }

async function runBulkInvite() {
  let cands;
  try { cands = JSON.parse(document.getElementById('bulk-candidates').value); }
  catch { showToast('Invalid JSON','error'); return; }
  const res = await api('POST', '/api/bulk/invite', {
    interview_id: document.getElementById('bulk-interview').value,
    candidates: cands,
  });
  if (res.operation_id) {
    showToast(`Invited ${res.created}, failed ${res.failed}`);
    document.getElementById('bulk-invite-modal').style.display='none';
    renderBulkOps();
  } else showToast(res.error||'Failed','error');
}

async function bulkRemind(interviewId) {
  const res = await api('POST', '/api/bulk/remind', { interview_id: interviewId });
  if (res.operation_id) showToast(`Reminders sent to ${res.reminded} candidates`);
  else showToast(res.error||'Failed','error');
}

async function createWorkflow() {
  const res = await api('POST', '/api/workflows', {
    name: document.getElementById('wf-name').value,
    trigger_type: document.getElementById('wf-trigger').value,
    action_type: document.getElementById('wf-action').value,
  });
  if (res.workflow) { showToast('Workflow created!'); document.getElementById('workflow-modal').style.display='none'; renderBulkOps(); }
  else showToast(res.error||'Failed','error');
}

async function deleteWorkflow(id) {
  if (!confirm('Delete this workflow rule?')) return;
  await api('DELETE', `/api/workflows/${id}`);
  showToast('Deleted'); renderBulkOps();
}


// ==================== CYCLE 18: AUDIT TRAIL & COMPLIANCE ====================

async function renderAuditTrail() {
  const [auditRes, statsRes, retentionRes] = await Promise.all([
    api('GET', '/api/audit/log?limit=25'),
    api('GET', '/api/audit/stats'),
    api('GET', '/api/compliance/retention'),
  ]);
  const entries = auditRes.entries || [];
  const stats = statsRes || {};
  const policies = retentionRes.policies || [];

  content.innerHTML = `
    <div class="page-header"><h1>Activity History</h1></div>
    <div class="stats-grid" style="grid-template-columns:repeat(4,1fr);margin-bottom:24px">
      <div class="stat-card"><div class="stat-value">${stats.total||0}</div><div class="stat-label">Audit Entries</div></div>
      <div class="stat-card"><div class="stat-value">${(stats.by_severity||{}).warning||0}</div><div class="stat-label">Warnings</div></div>
      <div class="stat-card"><div class="stat-value">${(stats.by_severity||{}).error||0}</div><div class="stat-label">Errors</div></div>
      <div class="stat-card"><div class="stat-value">${policies.length}</div><div class="stat-label">Retention Policies</div></div>
    </div>
    <div style="display:grid;grid-template-columns:2fr 1fr;gap:16px;margin-bottom:24px">
      <div class="card"><div class="card-header"><h3>Audit Log</h3></div><div class="card-body" style="max-height:400px;overflow-y:auto">
        ${entries.length===0?'<p style="color:#999">No audit entries yet. Actions will be logged automatically.</p>':
        '<table class="data-table"><thead><tr><th>Time</th><th>Actor</th><th>Action</th><th>Resource</th><th>Severity</th></tr></thead><tbody>'+
        entries.map(e=>`<tr><td style="white-space:nowrap;font-size:12px">${new Date(e.created_at).toLocaleString()}</td>
          <td>${e.actor_name||'-'}</td><td>${e.action}</td>
          <td>${e.resource_type}${e.resource_name?' / '+e.resource_name:''}</td>
          <td><span class="badge ${e.severity==='error'?'badge-danger':e.severity==='warning'?'badge-warning':'badge-info'}">${e.severity}</span></td></tr>`).join('')+
        '</tbody></table>'}
      </div></div>
      <div class="card"><div class="card-header"><h3>Retention Policies</h3>
        <button class="btn btn-sm btn-primary" onclick="showCreateRetentionModal()">+ Policy</button></div>
      <div class="card-body">
        ${policies.length===0?'<p style="color:#999">No retention policies set.</p>':
        policies.map(p=>`<div style="padding:8px 0;border-bottom:1px solid #eee">
          <strong>${p.resource_type}</strong><br>
          <span style="color:#666;font-size:12px">${p.retention_days} days, auto-delete: ${p.auto_delete?'Yes':'No'}</span>
          <button class="btn btn-sm btn-danger" style="float:right" onclick="deleteRetentionPolicy('${p.id}')">Delete</button>
        </div>`).join('')}
      </div></div>
    </div>
    <div id="retention-modal" style="display:none;position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1000;align-items:center;justify-content:center">
      <div style="background:#fff;border-radius:12px;padding:24px;width:400px">
        <h3>Create Retention Policy</h3>
        <div style="margin:12px 0"><label>Resource Type</label><select id="ret-type" class="form-input">
          <option value="candidates">Candidates</option><option value="responses">Responses</option>
          <option value="reports">Reports</option><option value="audit_log">Audit Log</option></select></div>
        <div style="margin:12px 0"><label>Retention Days</label><input id="ret-days" type="number" class="form-input" value="365"></div>
        <div style="margin:12px 0"><label><input type="checkbox" id="ret-auto"> Auto-delete after retention period</label></div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:16px">
          <button class="btn btn-outline" onclick="document.getElementById('retention-modal').style.display='none'">Cancel</button>
          <button class="btn btn-primary" onclick="createRetentionPolicy()">Create</button></div>
      </div></div>`;
}

function showCreateRetentionModal() { document.getElementById('retention-modal').style.display='flex'; }

async function createRetentionPolicy() {
  const res = await api('POST', '/api/compliance/retention', {
    resource_type: document.getElementById('ret-type').value,
    retention_days: parseInt(document.getElementById('ret-days').value),
    auto_delete: document.getElementById('ret-auto').checked,
  });
  if (res.policy) { showToast('Policy created!'); document.getElementById('retention-modal').style.display='none'; renderAuditTrail(); }
  else showToast(res.error||'Failed','error');
}

async function deleteRetentionPolicy(id) {
  if (!confirm('Delete this retention policy?')) return;
  await api('DELETE', `/api/compliance/retention/${id}`);
  showToast('Deleted'); renderAuditTrail();
}


// ==================== CYCLE 19: BILLING & SUBSCRIPTION UI ====================

// ==================== CYCLE 30: BILLING & SUBSCRIPTION ====================

async function renderBilling() {
  const el = document.getElementById('page-content');
  el.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading billing...</div>';

  let usage = {}, status = {};
  try {
    [usage, status] = await Promise.all([
      api('/api/billing/usage'),
      api('/api/billing/status')
    ]);
  } catch(e) { usage = {}; status = {}; }

  const rawPlan = usage.plan || status.plan || 'free';
  const isTrial = usage.is_trial;
  const trialDays = usage.trial_days_remaining || 0;
  // Trial users get Professional features — map for display
  const plan = (rawPlan === 'trial' || (isTrial && rawPlan === 'professional')) ? 'professional' : rawPlan;
  const planLabel = isTrial ? 'Professional Trial' : (plan === 'professional' ? 'Professional' : plan.charAt(0).toUpperCase() + plan.slice(1));
  const price = usage.price || 0;
  const subStatus = usage.subscription_status || status.subscription_status || 'none';

  const plans = [
    {id:'free', name:'Free', price:0, desc:'Get started with basics', candidates:5, interviews:1, seats:1, storage:'500MB',
     features:['Basic pipeline','1 interview template','Email support'], cta:'Current' },
    {id:'starter', name:'Starter', price:99, desc:'For growing agencies', candidates:50, interviews:5, seats:3, storage:'5GB',
     features:['Team collaboration','Multiple pipelines','Email templates','Priority support'], cta:'Upgrade', popular:false },
    {id:'professional', name:'Professional', price:179, desc:'Full recruiting power', candidates:200, interviews:25, seats:10, storage:'25GB',
     features:['AI candidate scoring','Advanced analytics','Bulk operations','API access','Integrations'], cta:'Upgrade', popular:true },
    {id:'enterprise', name:'Enterprise', price:299, desc:'Unlimited everything', candidates:-1, interviews:-1, seats:-1, storage:'Unlimited',
     features:['Unlimited everything','White-label branding','Custom workflows','Dedicated support','SLA guarantee'], cta:'Upgrade' },
  ];

  function meter(label, used, limit, pct) {
    const color = pct > 90 ? '#dc2626' : pct > 70 ? '#f59e0b' : '#0ace0a';
    const limitText = limit < 0 ? 'Unlimited' : limit;
    return `<div class="card" style="flex:1;min-width:180px">
      <div style="font-size:12px;color:#888;text-transform:uppercase;letter-spacing:0.5px;margin-bottom:6px">${label}</div>
      <div style="font-size:22px;font-weight:700">${used} <span style="font-size:13px;color:#999;font-weight:400">/ ${limitText}</span></div>
      <div style="height:6px;background:#f3f4f6;border-radius:3px;margin-top:10px;overflow:hidden">
        <div style="height:100%;width:${limit < 0 ? 5 : pct}%;background:${color};border-radius:3px;transition:width 0.3s"></div>
      </div>
      ${pct >= 90 && limit > 0 ? '<div style="font-size:11px;color:#dc2626;margin-top:4px">Approaching limit</div>' : ''}
    </div>`;
  }

  el.innerHTML = `
    <div class="page-header"><div><h1>Billing & Subscription</h1><p class="subtitle">Manage your plan and usage</p></div></div>

    <!-- Current Plan Banner -->
    <div class="card" style="background:linear-gradient(135deg,#111 0%,#1a1a1a 100%);color:#fff;margin-bottom:24px">
      <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:16px">
        <div>
          <div style="font-size:12px;text-transform:uppercase;letter-spacing:1px;color:#0ace0a;font-weight:600">Current Plan</div>
          <div style="font-size:28px;font-weight:700;margin:4px 0">${planLabel} ${price > 0 ? '<span style="font-size:16px;color:#999;font-weight:400">$'+price+'/mo</span>' : ''}</div>
          <div style="color:#999">${isTrial ? '30-day Professional Trial · No card required' : subStatus === 'active' ? 'Active subscription' : 'Free tier'}</div>
          ${isTrial ? `<div style="color:#f59e0b;margin-top:4px;font-size:14px">⏱ Trial ends in ${trialDays} day${trialDays!==1?'s':''}</div>` : ''}
        </div>
        <div style="display:flex;gap:8px">
          ${status.has_subscription ? '<button class="btn" style="background:#333;color:#fff;border:1px solid #555" onclick="openBillingPortal()">Manage Subscription</button>' : ''}
        </div>
      </div>
    </div>

    <!-- Usage Meters -->
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin-bottom:24px">
      ${meter('Candidates This Month', usage.candidates_used||0, usage.candidates_limit||5, usage.candidates_pct||0)}
      ${meter('Interviews', usage.interviews_used||0, usage.interviews_limit||1, usage.interviews_pct||0)}
      ${meter('Team Seats', usage.team_seats_used||1, usage.team_seats_limit||1, usage.team_seats_limit > 0 ? Math.round((usage.team_seats_used||1)/(usage.team_seats_limit||1)*100) : 0)}
      ${meter('Video Storage', (usage.storage_used_mb||0) > 1024 ? ((usage.storage_used_mb/1024).toFixed(1)+'GB') : ((usage.storage_used_mb||0)+'MB'), usage.storage_limit_mb > 0 ? (usage.storage_limit_mb >= 1024 ? (usage.storage_limit_mb/1024)+'GB' : usage.storage_limit_mb+'MB') : 'Unlimited', usage.storage_pct||0)}
    </div>

    <!-- Feature Access -->
    <div class="card" style="margin-bottom:24px">
      <h3 style="margin:0 0 12px;font-size:15px">Feature Access</h3>
      <div style="display:flex;gap:20px;flex-wrap:wrap">
        ${[['AI Scoring', usage.features?.ai_scoring],['API Access', usage.features?.api_access],['Connections', usage.features?.integrations],['Bulk Actions', usage.features?.bulk_ops],['Custom Look & Feel', usage.features?.white_label]].map(([f,on]) =>
          `<div style="font-size:13px;color:${on?'#0ace0a':'#ccc'}">${on?'✓':'✗'} ${f}</div>`
        ).join('')}
      </div>
    </div>

    <!-- Plans Grid -->
    <h2 style="margin:0 0 16px;font-size:20px">Available Plans</h2>
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:16px;margin-bottom:32px">
      ${plans.map(p => `
        <div class="card" style="border:${p.id===plan ? '2px solid #0ace0a' : p.popular ? '2px solid #111' : '1px solid #e5e7eb'};position:relative;display:flex;flex-direction:column">
          ${p.id===plan ? `<div style="position:absolute;top:-1px;right:12px;background:#0ace0a;color:#000;font-size:10px;font-weight:700;padding:2px 8px;border-radius:0 0 6px 6px">${isTrial && p.id==='professional' ? 'TRIAL' : 'CURRENT'}</div>` : ''}
          ${p.popular && p.id!==plan ? '<div style="position:absolute;top:-1px;right:12px;background:#111;color:#fff;font-size:10px;font-weight:700;padding:2px 8px;border-radius:0 0 6px 6px">POPULAR</div>' : ''}
          <div style="font-size:18px;font-weight:700;margin-bottom:4px">${p.name}</div>
          <div style="color:#666;font-size:13px;margin-bottom:12px">${p.desc}</div>
          <div style="font-size:32px;font-weight:800;margin-bottom:12px">
            ${p.price === 0 ? 'Free' : '$'+p.price}<span style="font-size:13px;font-weight:400;color:#999">${p.price > 0 ? '/mo' : ''}</span>
          </div>
          <div style="font-size:12px;color:#666;margin-bottom:12px;line-height:1.6">
            ${p.candidates < 0 ? 'Unlimited' : p.candidates} candidates/mo<br>
            ${p.interviews < 0 ? 'Unlimited' : p.interviews} interviews<br>
            ${p.seats < 0 ? 'Unlimited' : p.seats} team seat${p.seats!==1?'s':''}<br>
            ${p.storage} storage
          </div>
          <div style="flex:1;margin-bottom:16px">
            ${p.features.map(f => `<div style="padding:3px 0;font-size:12px;color:#555"><span style="color:#0ace0a;font-weight:700">✓</span> ${f}</div>`).join('')}
          </div>
          ${p.id === plan ? '<button class="btn btn-outline" style="width:100%" disabled>Current Plan</button>'
            : p.price > 0 ? `<button class="btn btn-primary" style="width:100%" onclick="upgradeToPlan('${p.id}')">Upgrade to ${p.name}</button>`
            : `<button class="btn btn-outline" style="width:100%" onclick="upgradeToPlan('${p.id}')">Downgrade</button>`}
        </div>
      `).join('')}
    </div>

    ${status.has_subscription ? `
    <div class="card">
      <h3 style="margin:0 0 12px;font-size:15px">Manage Subscription</h3>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn btn-outline" onclick="openBillingPortal()">Update Payment Method</button>
        <button class="btn btn-outline" onclick="openBillingPortal()">View Invoices</button>
      </div>
    </div>
    ` : `
    <div class="card" style="background:#f8faf8;text-align:center;padding:32px">
      <p style="color:#666;font-size:14px;margin:0">No credit card required for free tier. Upgrade anytime to unlock more features.</p>
    </div>
    `}
  `;
}

async function upgradeToPlan(planId) {
  if (planId === 'free') {
    if (!confirm('Downgrade to Free? You will lose access to paid features at the end of your billing period.')) return;
    try {
      const res = await api('POST', '/api/billing/upgrade', { plan: planId });
      if (res.success) { clearPlanCache(); toast('Plan changed to Free', 'success'); renderBilling(); }
      else toast(res.error || 'Failed', 'error');
    } catch(e) { toast(e.message, 'error'); }
    return;
  }
  // Try Stripe checkout first, fall back to direct upgrade
  try {
    const res = await api('POST', '/api/billing/checkout', { plan: planId });
    if (res.checkout_url) {
      window.location.href = res.checkout_url;
    } else {
      // No Stripe — do direct plan upgrade
      const up = await api('POST', '/api/billing/upgrade', { plan: planId });
      if (up.success) { clearPlanCache(); toast('Upgraded to ' + planId + '!', 'success'); renderBilling(); }
      else toast(up.error || 'Upgrade failed', 'error');
    }
  } catch(e) {
    // Stripe not configured — try direct upgrade
    try {
      const up = await api('POST', '/api/billing/upgrade', { plan: planId });
      if (up.success) { clearPlanCache(); toast('Upgraded to ' + planId + '!', 'success'); renderBilling(); }
      else toast(up.error || 'Upgrade failed', 'error');
    } catch(e2) { toast(e2.message || 'Upgrade failed', 'error'); }
  }
}

async function openBillingPortal() {
  try {
    const res = await api('POST', '/api/billing/portal');
    if (res.portal_url) window.location.href = res.portal_url;
    else toast('Could not open billing portal', 'error');
  } catch(e) { toast(e.message, 'error'); }
}


// ==================== CYCLE 19: VIDEO LIBRARY UI ====================

async function renderVideoLibrary() {
  const el = document.getElementById('page-content');
  el.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading video library...</div>';

  let assets = {}, stats = {};
  try {
    [assets, stats] = await Promise.all([
      api('GET', '/api/videos'),
      api('GET', '/api/storage/stats')
    ]);
  } catch(e) { assets = {assets:[]}; stats = {}; }

  const videos = assets.assets || [];

  el.innerHTML = `
    <div class="page-header">
      <h1>Video Library</h1>
      <div style="display:flex;gap:8px">
        <span class="badge badge-primary">${videos.length} videos</span>
        <span class="badge badge-gray">${stats.total_mb||0} MB used</span>
      </div>
    </div>

    <!-- Storage Stats -->
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px">
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:#0ace0a">${stats.total_videos||0}</div>
        <div style="font-size:13px;color:#666">Total Videos</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700">${stats.total_mb||0} MB</div>
        <div style="font-size:13px;color:#666">Storage Used</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700">${stats.max_storage_gb||5} GB</div>
        <div style="font-size:13px;color:#666">Storage Limit</div>
      </div>
      <div class="card" style="text-align:center">
        <div style="font-size:28px;font-weight:700;color:${(stats.usage_pct||0) > 80 ? '#dc2626' : '#0ace0a'}">${stats.usage_pct||0}%</div>
        <div style="font-size:13px;color:#666">Usage</div>
      </div>
    </div>

    <!-- Storage by Interview -->
    ${stats.by_interview?.length ? `
    <h2 style="margin:16px 0 12px">Storage by Interview</h2>
    <div class="card" style="margin-bottom:24px">
      <table class="data-table"><thead><tr><th>Interview</th><th>Videos</th><th>Size</th></tr></thead><tbody>
      ${stats.by_interview.map(r => `<tr>
        <td>${r.title||r.interview_id||'—'}</td>
        <td>${r.video_count}</td>
        <td>${r.total_bytes ? (r.total_bytes/(1024*1024)).toFixed(1)+' MB' : '0 MB'}</td>
      </tr>`).join('')}
      </tbody></table>
    </div>` : ''}

    <!-- Video Assets Table -->
    <h2 style="margin:16px 0 12px">All Videos</h2>
    <div class="card">
      ${videos.length ? `
        <table class="data-table"><thead><tr><th>Video</th><th>Interview</th><th>Size</th><th>Status</th><th>Uploaded</th><th>Actions</th></tr></thead><tbody>
        ${videos.map(v => `<tr>
          <td><a href="#" onclick="playVideo('${v.id}')" style="color:#0ace0a">${v.original_filename || v.id.slice(0,8)}</a></td>
          <td>${v.interview_id ? v.interview_id.slice(0,8)+'...' : '—'}</td>
          <td>${v.file_size ? (v.file_size/(1024*1024)).toFixed(1)+' MB' : '—'}</td>
          <td>${statusBadge(v.transcode_status || 'pending')}</td>
          <td>${formatDate(v.uploaded_at)}</td>
          <td>
            <button class="btn btn-sm btn-outline" onclick="transcodeVideo('${v.id}')" title="Transcode">MP4</button>
            <button class="btn btn-sm btn-danger" onclick="deleteVideoAsset('${v.id}')">Delete</button>
          </td>
        </tr>`).join('')}
        </tbody></table>
      ` : '<p style="color:#999;text-align:center;padding:24px">No videos uploaded yet.</p>'}
    </div>
  `;
}

async function playVideo(assetId) {
  try {
    const data = await api('GET', `/api/videos/${assetId}/stream`);
    if (data.stream_url) window.open(data.stream_url, '_blank');
  } catch(e) { showToast(e.message, 'error'); }
}

async function transcodeVideo(assetId) {
  try {
    const res = await api('POST', `/api/videos/${assetId}/transcode`);
    showToast(res.message || 'Transcoding started', 'success');
  } catch(e) { showToast(e.message, 'error'); }
}

async function deleteVideoAsset(assetId) {
  if (!confirm('Delete this video permanently?')) return;
  try {
    await api('DELETE', `/api/videos/${assetId}`);
    showToast('Video deleted');
    renderVideoLibrary();
  } catch(e) { showToast(e.message, 'error'); }
}


// ==================== CYCLE 20: AI INSIGHTS UI ====================

async function renderAiInsightsC20() {
  const el = document.getElementById('page-content');
  el.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading AI Insights...</div>';

  let interviews = [];
  try {
    const data = await api('GET', '/api/interviews');
    interviews = data.interviews || data || [];
  } catch(e) { interviews = []; }

  el.innerHTML = `
    <div class="page-header"><h1>AI Scoring</h1></div>

    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:16px;margin-bottom:24px">
      <div class="card" style="border-left:4px solid #0ace0a">
        <h3 style="font-size:16px;margin-bottom:8px">Score a Candidate</h3>
        <p style="color:#666;font-size:13px;margin-bottom:12px">Run AI analysis on a candidate's interview responses to generate a scorecard.</p>
        <button class="btn btn-primary" onclick="showAiScoreModal()">Score Candidate</button>
      </div>
      <div class="card" style="border-left:4px solid #f59e0b">
        <h3 style="font-size:16px;margin-bottom:8px">Batch Score</h3>
        <p style="color:#666;font-size:13px;margin-bottom:12px">Score all completed candidates in an interview at once.</p>
        <button class="btn btn-outline" onclick="showBatchScoreModal()">Batch Score</button>
      </div>
      <div class="card" style="border-left:4px solid #8b5cf6">
        <h3 style="font-size:16px;margin-bottom:8px">Transcribe Responses</h3>
        <p style="color:#666;font-size:13px;margin-bottom:12px">Convert video responses to text transcripts for AI scoring.</p>
        <button class="btn btn-outline" onclick="showTranscribeModal()">Transcribe</button>
      </div>
    </div>

    <h2 style="margin:16px 0 12px">Interview Insights</h2>
    ${Array.isArray(interviews) && interviews.length ? interviews.map(iv => `
      <div class="card" style="margin-bottom:12px;cursor:pointer" onclick="loadInterviewInsights('${iv.id}')">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div><strong>${iv.title||'Untitled'}</strong><div style="font-size:13px;color:#666">${iv.position||iv.department||''}</div></div>
          <button class="btn btn-sm btn-outline">View Insights</button>
        </div>
      </div>
    `).join('') : '<div class="card"><p style="color:#999;text-align:center;padding:20px">Create an interview and score candidates to see insights here.</p></div>'}

    <div id="insights-panel"></div>
  `;
}

async function loadInterviewInsights(interviewId) {
  const panel = document.getElementById('insights-panel');
  if (!panel) return;
  panel.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
  try {
    const data = await api('GET', `/api/ai/insights/${interviewId}`);
    if (!data.insights && !data.total_scored) {
      panel.innerHTML = '<div class="card"><p style="color:#999;text-align:center">No scorecards yet. Score candidates to generate insights.</p></div>';
      return;
    }
    panel.innerHTML = `<div class="card" style="margin-top:16px">
      <h3>Interview Insights</h3>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:12px;margin:16px 0">
        <div style="text-align:center"><div style="font-size:24px;font-weight:700;color:#0ace0a">${data.total_scored||0}</div><div style="font-size:12px;color:#666">Scored</div></div>
        <div style="text-align:center"><div style="font-size:24px;font-weight:700;color:#16a34a">${data.insights?.hire_rate||0}%</div><div style="font-size:12px;color:#666">Hire Rate</div></div>
        <div style="text-align:center"><div style="font-size:24px;font-weight:700">${data.recommendations?.['Strong Hire']||0}</div><div style="font-size:12px;color:#666">Strong Hires</div></div>
      </div>
      ${data.top_candidates?.length ? `<p style="font-size:13px"><strong>Top candidates:</strong> ${data.top_candidates.join(', ')}</p>` : ''}
    </div>`;
  } catch(e) { panel.innerHTML = `<div class="card"><p style="color:#dc2626">${e.message}</p></div>`; }
}

function showAiScoreModal() { showToast('Select a candidate from the Candidates page and click Score', 'info'); }
function showBatchScoreModal() { showToast('Select an interview with completed candidates to batch score', 'info'); }
function showTranscribeModal() { showToast('Transcription requires completed video responses', 'info'); }


// ==================== CYCLE 20: NOTIFICATION SETTINGS UI ====================

async function renderNotificationSettings() {
  const el = document.getElementById('page-content');
  el.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';

  let settings = {}, notifs = {};
  try {
    [settings, notifs] = await Promise.all([
      api('GET', '/api/notifications/settings'),
      api('GET', '/api/notifications')
    ]);
  } catch(e) { settings = {}; notifs = {}; }

  el.innerHTML = `
    <div class="page-header"><h1>Notification Settings</h1></div>

    <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
      <div>
        <div class="card">
          <h3 style="margin-bottom:16px">Preferences</h3>
          <div style="display:flex;flex-direction:column;gap:12px">
            ${notifToggle('email_on_candidate_complete', 'Email on candidate completion', settings)}
            ${notifToggle('email_on_new_score', 'Email on new AI score', settings)}
            ${notifToggle('email_daily_digest', 'Daily email digest', settings)}
            ${notifToggle('push_enabled', 'Push notifications', settings)}
            ${notifToggle('in_app_enabled', 'In-app notifications', settings)}
          </div>
          <div style="margin-top:16px;display:flex;gap:8px">
            <button class="btn btn-primary" onclick="saveNotifSettings()">Save Settings</button>
            <button class="btn btn-outline" onclick="sendTestNotif()">Send Test</button>
          </div>
        </div>

        <div class="card" style="margin-top:16px">
          <h3 style="margin-bottom:12px">Outgoing Webhooks</h3>
          <div id="webhooks-list"></div>
          <button class="btn btn-outline" style="margin-top:12px" onclick="showAddWebhookModal()">+ Add Webhook</button>
        </div>
      </div>

      <div class="card">
        <h3 style="margin-bottom:12px">Recent Notifications <span class="badge badge-blue">${notifs.unread_count||0} unread</span></h3>
        <button class="btn btn-sm btn-outline" style="margin-bottom:12px" onclick="markAllRead()">Mark All Read</button>
        <div style="max-height:400px;overflow-y:auto">
          ${(notifs.notifications||[]).slice(0,20).map(n => `
            <div style="padding:10px;border-bottom:1px solid #f3f4f6;opacity:${n.read_at ? 0.6 : 1}">
              <div style="font-weight:${n.read_at ? 400 : 600};font-size:14px">${n.title||n.message||'Notification'}</div>
              <div style="font-size:12px;color:#999">${formatDate(n.created_at)}</div>
            </div>
          `).join('') || '<p style="color:#999;padding:20px;text-align:center">No notifications yet.</p>'}
        </div>
      </div>
    </div>
  `;

  loadOutgoingWebhooks();
}

function notifToggle(key, label, settings) {
  return `<label style="display:flex;align-items:center;gap:8px;cursor:pointer">
    <input type="checkbox" id="notif-${key}" ${settings[key] ? 'checked' : ''}>
    <span style="font-size:14px">${label}</span>
  </label>`;
}

async function saveNotifSettings() {
  const settings = {};
  document.querySelectorAll('[id^="notif-"]').forEach(el => {
    settings[el.id.replace('notif-', '')] = el.checked;
  });
  try {
    await api('PUT', '/api/notifications/settings', settings);
    showToast('Settings saved!', 'success');
  } catch(e) { showToast(e.message, 'error'); }
}

async function sendTestNotif() {
  try {
    await api('POST', '/api/notifications/test');
    showToast('Test notification sent!', 'success');
    setTimeout(() => renderNotificationSettings(), 1000);
  } catch(e) { showToast(e.message, 'error'); }
}

async function markAllRead() {
  try {
    await api('POST', '/api/notifications/read-all');
    showToast('All marked read');
    renderNotificationSettings();
  } catch(e) { showToast(e.message, 'error'); }
}

async function loadOutgoingWebhooks() {
  const el = document.getElementById('webhooks-list');
  if (!el) return;
  try {
    const data = await api('GET', '/api/webhooks/outgoing');
    el.innerHTML = (data.webhooks||[]).map(h => `
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f3f4f6">
        <div><code style="font-size:12px">${h.url}</code></div>
        <button class="btn btn-sm btn-danger" onclick="deleteOutgoingWebhook('${h.id}')">Remove</button>
      </div>
    `).join('') || '<p style="color:#999;font-size:13px">No webhooks configured.</p>';
  } catch(e) { el.innerHTML = ''; }
}

function showAddWebhookModal() {
  const url = prompt('Enter webhook URL:');
  if (url) createOutgoingWebhook(url);
}

async function createOutgoingWebhook(url) {
  try {
    await api('POST', '/api/webhooks/outgoing', {url, events: ['candidate_completed','score_generated']});
    showToast('Webhook added!', 'success');
    loadOutgoingWebhooks();
  } catch(e) { showToast(e.message, 'error'); }
}

async function deleteOutgoingWebhook(id) {
  if (!confirm('Remove this webhook?')) return;
  try {
    await api('DELETE', `/api/webhooks/outgoing/${id}`);
    showToast('Webhook removed');
    loadOutgoingWebhooks();
  } catch(e) { showToast(e.message, 'error'); }
}


// ==================== CYCLE 21: AMS INTEGRATIONS ====================

async function renderAmsIntegrations() {
  const providers = await api('GET', '/api/ams/providers');
  const connections = await api('GET', '/api/ams/connections');
  const connList = connections.connections || [];
  const connMap = {};
  connList.forEach(c => connMap[c.provider] = c);

  content.innerHTML = `
    <div style="max-width:900px">
      <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Connect Your AMS / CRM</h2>
      <p style="color:#666;margin-bottom:24px">Sync your candidates and interview info with the management system you already use.</p>

      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:16px;margin-bottom:32px">
        ${(providers.providers||[]).map(p => {
          const conn = connMap[p.slug];
          const isConnected = conn && conn.status === 'connected';
          return `
            <div style="border:1px solid ${isConnected ? '#0ace0a' : '#e5e7eb'};border-radius:12px;padding:20px;background:${isConnected ? '#f0fff0' : '#fff'}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h3 style="font-size:16px;font-weight:600">${p.name}</h3>
                <span style="font-size:12px;padding:3px 8px;border-radius:12px;background:${isConnected ? '#0ace0a' : '#f3f4f6'};color:${isConnected ? '#fff' : '#666'}">${isConnected ? 'Connected' : 'Available'}</span>
              </div>
              <p style="font-size:13px;color:#666;margin-bottom:12px">${p.description}</p>
              <p style="font-size:12px;color:#999;margin-bottom:12px">Syncs: ${p.supported_sync.join(', ')}</p>
              ${isConnected
                ? `<div style="display:flex;gap:8px">
                     <button class="btn btn-sm btn-primary" onclick="syncAms('${conn.id}')">Sync Now</button>
                     <button class="btn btn-sm btn-outline" onclick="viewSyncLogs('${conn.id}')">Logs</button>
                     <button class="btn btn-sm btn-danger" onclick="disconnectAms('${conn.id}')">Disconnect</button>
                   </div>`
                : `<button class="btn btn-sm btn-primary" onclick="connectAms('${p.slug}','${p.name}')">Connect</button>`
              }
            </div>`;
        }).join('')}
      </div>

      <div id="sync-logs-panel"></div>
    </div>`;
}

async function connectAms(slug, name) {
  const apiKey = prompt(`Enter your ${name} API key:`);
  if (!apiKey) return;
  try {
    await api('POST', '/api/ams/connections', {provider: slug, api_key: apiKey});
    showToast(`Connected to ${name}!`, 'success');
    renderAmsIntegrations();
  } catch(e) { showToast(e.message, 'error'); }
}

async function disconnectAms(connId) {
  if (!confirm('Disconnect this AMS integration?')) return;
  try {
    await api('DELETE', `/api/ams/connections/${connId}`);
    showToast('Disconnected', 'success');
    renderAmsIntegrations();
  } catch(e) { showToast(e.message, 'error'); }
}

async function syncAms(connId) {
  try {
    const result = await api('POST', `/api/ams/connections/${connId}/sync`, {sync_type: 'full'});
    showToast(`Synced ${result.records_synced} records from ${result.provider}`, 'success');
  } catch(e) { showToast(e.message, 'error'); }
}

async function viewSyncLogs(connId) {
  const el = document.getElementById('sync-logs-panel');
  try {
    const data = await api('GET', `/api/ams/connections/${connId}/logs`);
    el.innerHTML = `
      <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Sync History</h3>
      <table class="table"><thead><tr><th>Date</th><th>Type</th><th>Records</th><th>Status</th></tr></thead>
      <tbody>${(data.logs||[]).map(l => `
        <tr><td>${new Date(l.started_at).toLocaleString()}</td><td>${l.sync_type}</td>
        <td>${l.records_synced} synced / ${l.records_failed} failed</td>
        <td><span style="color:${l.status==='completed'?'#0ace0a':'#ef4444'}">${l.status}</span></td></tr>
      `).join('')}</tbody></table>`;
  } catch(e) { el.innerHTML = ''; }
}


// ==================== CYCLE 21: API MANAGEMENT ====================

async function renderApiManagement() {
  const keysData = await api('GET', '/api/keys/all');
  const keys = keysData.api_keys || [];

  content.innerHTML = `
    <div style="max-width:900px">
      <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">API Keys</h2>
      <p style="color:#666;margin-bottom:24px">Manage your API keys for connecting ChannelView to custom tools and automations.</p>

      <button class="btn btn-primary" onclick="showCreateKeyModal()" style="margin-bottom:20px">+ Create API Key</button>

      <div id="api-keys-list">
        ${keys.length === 0 ? '<p style="color:#999">No API keys yet. Create one to get started.</p>' :
          `<table class="table">
            <thead><tr><th>Name</th><th>Key</th><th>Scopes</th><th>Rate Limit</th><th>Usage</th><th>Status</th><th>Actions</th></tr></thead>
            <tbody>${keys.map(k => `
              <tr>
                <td style="font-weight:500">${k.name}</td>
                <td><code style="font-size:12px">${k.key_preview}</code></td>
                <td>${(k.scopes||[]).map(s => `<span style="font-size:11px;padding:2px 6px;background:#e6fce6;border-radius:8px;margin-right:4px">${s}</span>`).join('')}</td>
                <td>${k.rate_limit_per_hour}/hr</td>
                <td>${k.usage_count} requests</td>
                <td><span style="color:${k.is_active ? '#0ace0a' : '#ef4444'}">${k.is_active ? 'Active' : 'Revoked'}</span></td>
                <td>
                  ${k.is_active ? `<button class="btn btn-sm btn-danger" onclick="revokeApiKey('${k.id}')">Revoke</button>` : ''}
                  <button class="btn btn-sm btn-outline" onclick="viewKeyUsage('${k.id}')" style="margin-left:4px">Usage</button>
                </td>
              </tr>`).join('')}
            </tbody>
          </table>`
        }
      </div>
      <div id="key-usage-panel" style="margin-top:20px"></div>
    </div>`;
}

function showCreateKeyModal() {
  const name = prompt('API key name (e.g., "AgencyBloc Integration"):');
  if (!name) return;
  const scopeStr = prompt('Scopes (comma-separated: read, write, admin):', 'read');
  const scopes = (scopeStr||'read').split(',').map(s => s.trim());
  createApiKeyC21(name, scopes);
}

async function createApiKeyC21(name, scopes) {
  try {
    const result = await api('POST', '/api/keys/create', {name, scopes, rate_limit_per_hour: 1000});
    alert(`API Key Created!\n\n${result.api_key}\n\nCopy this now — it will not be shown again.`);
    renderApiManagement();
  } catch(e) { showToast(e.message, 'error'); }
}

async function revokeApiKey(keyId) {
  if (!confirm('Revoke this API key? This cannot be undone.')) return;
  try {
    await api('DELETE', `/api/keys/${keyId}/revoke`);
    showToast('API key revoked', 'success');
    renderApiManagement();
  } catch(e) { showToast(e.message, 'error'); }
}

async function viewKeyUsage(keyId) {
  const el = document.getElementById('key-usage-panel');
  try {
    const data = await api('GET', `/api/keys/${keyId}/usage`);
    el.innerHTML = `
      <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">API Key Usage</h3>
      <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:16px">
        <div style="background:#f9fafb;padding:12px;border-radius:8px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#0ace0a">${data.requests_last_hour}</div>
          <div style="font-size:12px;color:#666">Last Hour</div>
        </div>
        <div style="background:#f9fafb;padding:12px;border-radius:8px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#0ace0a">${data.requests_last_24h}</div>
          <div style="font-size:12px;color:#666">Last 24h</div>
        </div>
        <div style="background:#f9fafb;padding:12px;border-radius:8px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#0ace0a">${data.requests_last_7d}</div>
          <div style="font-size:12px;color:#666">Last 7 Days</div>
        </div>
      </div>`;
  } catch(e) { el.innerHTML = ''; }
}


// ==================== CYCLE 21: DEMO MANAGER ====================

async function renderDemoManager() {
  const data = await api('GET', '/api/demo/environments');
  const envs = data.environments || [];

  content.innerHTML = `
    <div style="max-width:900px">
      <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Demo Environments</h2>
      <p style="color:#666;margin-bottom:24px">Create sandbox environments with seeded insurance agency data for FMO presentations and sales demos.</p>

      <div style="display:flex;gap:12px;margin-bottom:24px">
        <button class="btn btn-primary" onclick="createDemoEnv('minimal')">Quick Demo (3 candidates)</button>
        <button class="btn btn-primary" onclick="createDemoEnv('standard')">Standard Demo (15 candidates)</button>
        <button class="btn btn-outline" onclick="createDemoEnv('full')">Full Demo (50 candidates)</button>
      </div>

      ${envs.length === 0 ? '<div style="background:#f9fafb;padding:32px;border-radius:12px;text-align:center"><p style="color:#999">No demo environments yet. Create one to prepare for your next FMO presentation.</p></div>' :
        `<div style="display:grid;gap:16px">
          ${envs.map(e => `
            <div style="border:1px solid #e5e7eb;border-radius:12px;padding:20px;background:#fff">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <h3 style="font-size:16px;font-weight:600">${e.name}</h3>
                <span style="font-size:12px;padding:3px 8px;border-radius:12px;background:${e.status==='active'?'#e6fce6':'#fee2e2'};color:${e.status==='active'?'#065f46':'#991b1b'}">${e.status}</span>
              </div>
              <div style="display:flex;gap:16px;font-size:13px;color:#666;margin-bottom:12px">
                <span>Profile: ${e.seed_profile}</span>
                <span>Views: ${e.view_count}</span>
                <span>Expires: ${e.expires_at ? new Date(e.expires_at).toLocaleDateString() : 'Never'}</span>
              </div>
              <div style="display:flex;gap:8px">
                <button class="btn btn-sm btn-outline" onclick="copyDemoUrl('${e.slug}')">Copy Link</button>
                <button class="btn btn-sm btn-outline" onclick="resetDemoEnv('${e.id}')">Reset</button>
                <button class="btn btn-sm btn-danger" onclick="deleteDemoEnv('${e.id}')">Delete</button>
              </div>
            </div>
          `).join('')}
        </div>`
      }
    </div>`;
}

async function createDemoEnv(profile) {
  const name = prompt('Demo environment name:', `FMO Demo - ${new Date().toLocaleDateString()}`);
  if (!name) return;
  try {
    const result = await api('POST', '/api/demo/environments', {name, seed_profile: profile, expires_days: 30});
    showToast(`Demo created with ${result.environment.total_candidates} candidates!`, 'success');
    renderDemoManager();
  } catch(e) { showToast(e.message, 'error'); }
}

function copyDemoUrl(slug) {
  navigator.clipboard.writeText(window.location.origin + '/demo/' + slug);
  showToast('Demo link copied!', 'success');
}

async function resetDemoEnv(envId) {
  if (!confirm('Reset this demo? All seeded data will be cleared.')) return;
  try {
    await api('POST', `/api/demo/environments/${envId}/reset`);
    showToast('Demo reset', 'success');
    renderDemoManager();
  } catch(e) { showToast(e.message, 'error'); }
}

async function deleteDemoEnv(envId) {
  if (!confirm('Delete this demo environment permanently?')) return;
  try {
    await api('DELETE', `/api/demo/environments/${envId}`);
    showToast('Demo deleted', 'success');
    renderDemoManager();
  } catch(e) { showToast(e.message, 'error'); }
}


// ==================== CYCLE 22: ANALYTICS DASHBOARD ====================

async function renderAnalyticsDashboard() {
  const [overview, pipeline, interviews, trends, roi] = await Promise.all([
    api('GET', '/api/analytics/overview'),
    api('GET', '/api/analytics/pipeline'),
    api('GET', '/api/analytics/interviews'),
    api('GET', '/api/analytics/trends'),
    api('GET', '/api/analytics/roi'),
  ]);

  content.innerHTML = `
    <div style="max-width:1100px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Numbers at a Glance</h2>
          <p style="color:#666;font-size:13px">Your key recruiting numbers in one place</p>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm btn-outline" onclick="loadRoiPanel()">ROI Report</button>
        </div>
      </div>

      <!-- KPI Cards -->
      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:28px">
        ${kpiCard('Total Interviews', overview.total_interviews, overview.active_interviews + ' active')}
        ${kpiCard('Total Candidates', overview.total_candidates, overview.completion_rate + '% completion')}
        ${kpiCard('Avg AI Score', overview.avg_ai_score || 'N/A', 'across scored candidates')}
        ${kpiCard('Avg Time to Complete', overview.avg_time_to_complete_hours ? overview.avg_time_to_complete_hours + 'h' : 'N/A', 'from invite to submission')}
      </div>

      <!-- Pipeline Funnel -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Your Candidates</h3>
        <div style="display:flex;gap:4px;align-items:end;height:120px">
          ${(pipeline.pipeline||[]).map(s => {
            const maxCount = Math.max(...(pipeline.pipeline||[]).map(p => p.count), 1);
            const height = Math.max(s.count / maxCount * 100, 8);
            const colors = {invited:'#3b82f6',in_progress:'#f59e0b',completed:'#0ace0a',reviewed:'#8b5cf6',hired:'#10b981',rejected:'#ef4444'};
            return `<div style="flex:1;text-align:center">
              <div style="font-size:18px;font-weight:700;margin-bottom:4px">${s.count}</div>
              <div style="height:${height}px;background:${colors[s.stage]||'#ccc'};border-radius:6px 6px 0 0;min-height:8px"></div>
              <div style="font-size:11px;color:#666;margin-top:4px;text-transform:capitalize">${s.stage.replace('_',' ')}</div>
            </div>`;
          }).join('')}
        </div>
      </div>

      <!-- Trends + Per-Interview -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Weekly Activity (12 weeks)</h3>
          <div style="display:flex;gap:2px;align-items:end;height:80px">
            ${(trends.trends||[]).map(w => {
              const max = Math.max(...(trends.trends||[]).map(t => t.invited + t.completed), 1);
              const h = Math.max((w.invited + w.completed) / max * 70, 4);
              return `<div style="flex:1;background:#0ace0a;height:${h}px;border-radius:3px 3px 0 0;opacity:${w.invited+w.completed>0?1:0.2}" title="${w.week_start}: ${w.invited} invited, ${w.completed} completed"></div>`;
            }).join('')}
          </div>
          <div style="display:flex;justify-content:space-between;font-size:10px;color:#999;margin-top:4px">
            <span>${(trends.trends||[])[0]?.week_start||''}</span>
            <span>Now</span>
          </div>
        </div>

        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Top Interviews</h3>
          ${(interviews.interviews||[]).slice(0,5).map(i => `
            <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid #f3f4f6">
              <div style="font-size:13px;font-weight:500">${i.title}</div>
              <div style="display:flex;gap:12px;font-size:12px;color:#666">
                <span>${i.completion_rate}% done</span>
                <span>${i.total_candidates} cands</span>
                ${i.avg_ai_score ? `<span style="color:#0ace0a">${i.avg_ai_score} AI</span>` : ''}
              </div>
            </div>
          `).join('') || '<p style="color:#999;font-size:13px">No interviews yet.</p>'}
        </div>
      </div>

      <!-- ROI Panel -->
      <div id="roi-panel" style="background:#f0fff0;border:1px solid #0ace0a;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px;color:#065f46">ROI Summary</h3>
        <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px">
          ${roiCard('Candidates Screened', roi.total_candidates_screened)}
          ${roiCard('Total Hired', roi.total_hired)}
          ${roiCard('Time Saved', roi.time_saved_hours + ' hrs')}
          ${roiCard('Cost Per Hire', roi.cost_per_hire ? '$' + roi.cost_per_hire : 'N/A')}
        </div>
        <p style="font-size:12px;color:#666;margin-top:12px">${roi.time_saved_description} &middot; Plan: ${roi.plan} ($${roi.monthly_cost}/mo)</p>
      </div>
    </div>`;
}

function kpiCard(label, value, sub) {
  return `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:20px">
    <div style="font-size:12px;color:#666;margin-bottom:4px">${label}</div>
    <div style="font-size:28px;font-weight:700;color:#0ace0a">${value}</div>
    <div style="font-size:12px;color:#999;margin-top:2px">${sub}</div>
  </div>`;
}

function roiCard(label, value) {
  return `<div style="text-align:center">
    <div style="font-size:24px;font-weight:700;color:#065f46">${value}</div>
    <div style="font-size:12px;color:#666">${label}</div>
  </div>`;
}


// ==================== CYCLE 23: EMAIL DELIVERY ====================

async function renderEmailDelivery() {
  const [configRes, statsRes, logRes, templatesRes] = await Promise.all([
    apiFetch('/api/email/delivery-config'),
    apiFetch('/api/email/send-stats'),
    apiFetch('/api/email/delivery-log?limit=20'),
    apiFetch('/api/email/delivery-templates'),
  ]);
  const config = configRes || {};
  const stats = statsRes || {};
  const log = (logRes || {}).emails || [];
  const templates = (templatesRes || {}).templates || [];

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:1100px;margin:0 auto;padding:24px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h1 style="font-size:24px;font-weight:700">Email Delivery</h1>
          <p style="color:#666">Check if your emails to candidates are being sent and delivered</p>
        </div>
        <button class="btn btn-primary" onclick="showSendEmailModal()">Send Test Email</button>
      </div>

      <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:16px;margin-bottom:24px">
        ${emailStatCard('Total Sent', stats.total_sent || 0, '#0ace0a')}
        ${emailStatCard('Open Rate', (stats.open_rate || 0) + '%', '#2563eb')}
        ${emailStatCard('Click Rate', (stats.click_rate || 0) + '%', '#7c3aed')}
        ${emailStatCard('Bounce Rate', (stats.bounce_rate || 0) + '%', stats.bounce_rate > 5 ? '#dc2626' : '#059669')}
      </div>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px">
        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Provider Configuration</h3>
          <div style="margin-bottom:12px">
            <label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Email Provider</label>
            <select id="email-provider" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" onchange="updateEmailConfig()">
              <option value="internal" ${config.provider==='internal'?'selected':''}>Built-in (ChannelView)</option>
              <option value="sendgrid" ${config.provider==='sendgrid'?'selected':''}>SendGrid</option>
            </select>
          </div>
          <div style="margin-bottom:12px">
            <label style="font-size:13px;color:#666;display:block;margin-bottom:4px">From Name</label>
            <input id="email-from-name" type="text" value="${config.from_name || ''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="Your Agency Name">
          </div>
          <div style="margin-bottom:12px">
            <label style="font-size:13px;color:#666;display:block;margin-bottom:4px">From Address</label>
            <input id="email-from-addr" type="email" value="${config.from_address || ''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="hiring@youragency.com">
          </div>
          <div id="sendgrid-key-section" style="margin-bottom:12px;${config.provider!=='sendgrid'?'display:none':''}">
            <label style="font-size:13px;color:#666;display:block;margin-bottom:4px">SendGrid API Key</label>
            <input id="sendgrid-key" type="password" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="${config.sendgrid_configured ? '••••••••••••••••' : 'Enter API Key'}">
          </div>
          <button class="btn btn-primary" onclick="saveEmailConfig()">Save Configuration</button>
        </div>

        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Email Templates (${templates.length})</h3>
          ${templates.length === 0 ? '<p style="color:#999;font-size:14px">No custom templates yet. Default templates are used for invitations and notifications.</p>' :
            templates.map(t => `<div style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;margin-bottom:8px;display:flex;justify-content:space-between;align-items:center">
              <div><div style="font-weight:600;font-size:14px">${t.name}</div><div style="font-size:12px;color:#666">${t.template_type} — ${t.subject}</div></div>
              ${t.is_default ? '<span style="font-size:11px;background:#e5e7eb;padding:2px 8px;border-radius:4px">Default</span>' : ''}
            </div>`).join('')}
          <button class="btn btn-outline" style="margin-top:8px" onclick="showCreateTemplateModal()">+ New Template</button>
        </div>
      </div>

      <div class="card" style="padding:20px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Recent Emails</h3>
        <table style="width:100%;border-collapse:collapse;font-size:14px">
          <thead><tr style="border-bottom:2px solid #e5e7eb">
            <th style="text-align:left;padding:8px 12px;color:#666">Recipient</th>
            <th style="text-align:left;padding:8px 12px;color:#666">Subject</th>
            <th style="text-align:left;padding:8px 12px;color:#666">Status</th>
            <th style="text-align:left;padding:8px 12px;color:#666">Sent</th>
          </tr></thead>
          <tbody>
            ${log.length === 0 ? '<tr><td colspan="4" style="padding:24px;text-align:center;color:#999">No emails sent yet</td></tr>' :
              log.map(e => `<tr style="border-bottom:1px solid #f3f4f6">
                <td style="padding:8px 12px">${e.recipient_email}</td>
                <td style="padding:8px 12px">${e.subject}</td>
                <td style="padding:8px 12px"><span style="padding:2px 8px;border-radius:4px;font-size:12px;font-weight:600;background:${e.status==='sent'?'#dcfce7;color:#166534':e.status==='bounced'?'#fef2f2;color:#991b1b':'#f3f4f6;color:#374151'}">${e.status}</span></td>
                <td style="padding:8px 12px;color:#666">${e.sent_at ? new Date(e.sent_at).toLocaleDateString() : '—'}</td>
              </tr>`).join('')}
          </tbody>
        </table>
      </div>
    </div>`;
}

function emailStatCard(label, value, color) {
  return `<div class="card" style="padding:16px;text-align:center">
    <div style="font-size:28px;font-weight:700;color:${color}">${value}</div>
    <div style="font-size:13px;color:#666">${label}</div>
  </div>`;
}

async function saveEmailConfig() {
  const payload = {
    provider: document.getElementById('email-provider').value,
    from_name: document.getElementById('email-from-name').value,
    from_address: document.getElementById('email-from-addr').value,
  };
  const sgKey = document.getElementById('sendgrid-key');
  if (sgKey && sgKey.value && !sgKey.value.includes('•')) payload.sendgrid_api_key = sgKey.value;
  await apiFetch('/api/email/config', {method: 'PUT', body: JSON.stringify(payload)});
  showToast('Email configuration saved', 'success');
}

async function updateEmailConfig() {
  const section = document.getElementById('sendgrid-key-section');
  if (section) section.style.display = document.getElementById('email-provider').value === 'sendgrid' ? '' : 'none';
}

async function showSendEmailModal() {
  const modal = document.createElement('div');
  modal.id = 'send-email-modal';
  modal.innerHTML = `<div style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2000;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)this.remove()">
    <div style="background:#fff;border-radius:12px;padding:24px;width:480px;max-width:90vw">
      <h3 style="font-size:18px;font-weight:600;margin-bottom:16px">Send Test Email</h3>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">To Email</label><input id="test-email-to" type="email" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="test@example.com"></div>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Subject</label><input id="test-email-subject" type="text" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" value="Test Email from ChannelView"></div>
      <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn btn-outline" onclick="document.getElementById('send-email-modal').remove()">Cancel</button><button class="btn btn-primary" onclick="sendTestEmail()">Send</button></div>
    </div></div>`;
  document.body.appendChild(modal);
}

async function sendTestEmail() {
  const to = document.getElementById('test-email-to').value;
  const subject = document.getElementById('test-email-subject').value;
  if (!to) return showToast('Enter recipient email', 'error');
  await apiFetch('/api/email/deliver', {method:'POST', body: JSON.stringify({to_email:to, subject, template:'test'})});
  document.getElementById('send-email-modal')?.remove();
  showToast('Test email sent!', 'success');
  renderEmailDelivery();
}

function showCreateTemplateModal() {
  const modal = document.createElement('div');
  modal.id = 'create-template-modal';
  modal.innerHTML = `<div style="position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:2000;display:flex;align-items:center;justify-content:center" onclick="if(event.target===this)this.remove()">
    <div style="background:#fff;border-radius:12px;padding:24px;width:560px;max-width:90vw">
      <h3 style="font-size:18px;font-weight:600;margin-bottom:16px">Create Email Template</h3>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Template Name</label><input id="tpl-name" type="text" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="Candidate Invitation"></div>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Type</label><select id="tpl-type" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px"><option value="invitation">Invitation</option><option value="reminder">Reminder</option><option value="notification">Notification</option><option value="custom">Custom</option></select></div>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Subject Line</label><input id="tpl-subject" type="text" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="You're invited to interview for {{position}}"></div>
      <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Body (HTML)</label><textarea id="tpl-body" rows="6" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-family:monospace;font-size:13px" placeholder="<h2>Hello {{name}},</h2><p>...</p>"></textarea></div>
      <div style="display:flex;gap:8px;justify-content:flex-end"><button class="btn btn-outline" onclick="document.getElementById('create-template-modal').remove()">Cancel</button><button class="btn btn-primary" onclick="createEmailTemplate()">Create</button></div>
    </div></div>`;
  document.body.appendChild(modal);
}

async function createEmailTemplate() {
  const name = document.getElementById('tpl-name').value;
  const subject = document.getElementById('tpl-subject').value;
  const body_html = document.getElementById('tpl-body').value;
  const template_type = document.getElementById('tpl-type').value;
  if (!name || !subject || !body_html) return showToast('All fields required', 'error');
  await apiFetch('/api/email/delivery-templates', {method:'POST', body:JSON.stringify({name, subject, body_html, template_type})});
  document.getElementById('create-template-modal')?.remove();
  showToast('Template created', 'success');
  renderEmailDelivery();
}


// ==================== CYCLE 23: ONBOARDING WIZARD ====================

async function renderOnboardingWizard() {
  const res = await apiFetch('/api/onboarding/wizard-status');
  if (!res) return;
  const { steps, completion_percentage, is_completed, is_skipped } = res;

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:800px;margin:0 auto;padding:40px 24px">
      <div style="text-align:center;margin-bottom:32px">
        <h1 style="font-size:28px;font-weight:700;margin-bottom:8px">${is_completed || is_skipped ? 'Welcome Back!' : 'Welcome to ChannelView'}</h1>
        <p style="color:#666;font-size:16px">${is_completed ? 'Your setup is complete. You\'re ready to start hiring!' : is_skipped ? 'You skipped setup. Complete these steps anytime.' : 'Let\'s get your agency set up in a few quick steps.'}</p>
      </div>

      <div style="background:#f9fafb;border-radius:12px;padding:4px;margin-bottom:32px">
        <div style="background:#0ace0a;height:8px;border-radius:8px;width:${completion_percentage}%;transition:width .5s"></div>
      </div>
      <p style="text-align:center;font-size:14px;color:#666;margin-bottom:24px">${completion_percentage}% complete — ${steps.filter(s=>s.completed).length} of ${steps.length} steps done</p>

      <div style="display:flex;flex-direction:column;gap:12px">
        ${steps.map(s => `
          <div style="display:flex;align-items:center;gap:16px;padding:16px 20px;background:#fff;border:1px solid ${s.completed?'#0ace0a':'#e5e7eb'};border-radius:10px;${s.completed?'opacity:.7':''}">
            <div style="width:36px;height:36px;border-radius:50%;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:16px;flex-shrink:0;${s.completed?'background:#0ace0a;color:#fff':'background:#f3f4f6;color:#666'}">${s.completed ? '✓' : s.step}</div>
            <div style="flex:1">
              <div style="font-weight:600;font-size:15px">${s.name}</div>
              <div style="font-size:13px;color:#666">${s.description}</div>
            </div>
            ${s.completed
              ? '<span style="font-size:12px;color:#0ace0a;font-weight:600">Completed</span>'
              : `<button class="btn btn-primary btn-sm" onclick="completeOnboardingStep(${s.step})" style="padding:6px 16px;font-size:13px">Mark Done</button>`}
          </div>
        `).join('')}
      </div>

      ${!is_completed && !is_skipped ? `
        <div style="text-align:center;margin-top:24px">
          <button class="btn btn-outline" onclick="skipOnboarding()" style="font-size:14px">Skip Setup — I'll explore on my own</button>
        </div>` : ''}
    </div>`;
}

async function completeOnboardingStep(step) {
  await apiFetch('/api/onboarding/wizard-step', {method:'POST', body:JSON.stringify({step})});
  showToast('Step completed!', 'success');
  renderOnboardingWizard();
}

async function skipOnboarding() {
  await apiFetch('/api/onboarding/wizard-skip', {method:'POST'});
  showToast('Onboarding skipped. You can return anytime.', 'info');
  renderOnboardingWizard();
}


// ==================== CYCLE 23: HELP CENTER ====================

async function renderHelpCenter() {
  const [articlesRes, categoriesRes] = await Promise.all([
    apiFetch('/api/help/articles'),
    apiFetch('/api/help/categories'),
  ]);
  const articles = (articlesRes || {}).articles || [];
  const categories = (categoriesRes || {}).categories || [];

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:900px;margin:0 auto;padding:24px">
      <div style="text-align:center;margin-bottom:32px">
        <h1 style="font-size:28px;font-weight:700;margin-bottom:8px">Help Center</h1>
        <p style="color:#666">Find answers, guides, and tips for getting the most out of ChannelView</p>
        <div style="max-width:500px;margin:16px auto 0">
          <input id="help-search" type="text" placeholder="Search help articles..." style="width:100%;padding:12px 16px;border:1px solid #e5e7eb;border-radius:8px;font-size:15px" oninput="searchHelp(this.value)">
        </div>
      </div>

      <div style="display:flex;gap:8px;flex-wrap:wrap;justify-content:center;margin-bottom:24px">
        <button class="btn btn-sm ${!window._helpCategoryFilter?'btn-primary':'btn-outline'}" onclick="filterHelpCategory('')">All</button>
        ${categories.map(c => `<button class="btn btn-sm ${window._helpCategoryFilter===c.name?'btn-primary':'btn-outline'}" onclick="filterHelpCategory('${c.name}')">${c.name} (${c.count})</button>`).join('')}
      </div>

      <div id="help-articles-list">
        ${renderHelpArticlesList(articles)}
      </div>
    </div>`;
}

function renderHelpArticlesList(articles) {
  if (articles.length === 0) return '<p style="text-align:center;color:#999;padding:32px">No articles found</p>';
  return `<div style="display:flex;flex-direction:column;gap:12px">
    ${articles.map(a => `
      <div class="card" style="padding:16px 20px;cursor:pointer;border:1px solid #e5e7eb;border-radius:10px" onclick="viewHelpArticle('${a.slug}')">
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <div style="font-weight:600;font-size:15px;margin-bottom:4px">${a.title}</div>
            <span style="font-size:12px;background:#f3f4f6;padding:2px 8px;border-radius:4px;color:#666">${a.category}</span>
          </div>
          <svg viewBox="0 0 24 24" fill="none" stroke="#999" stroke-width="2" width="18" height="18"><polyline points="9 18 15 12 9 6"/></svg>
        </div>
      </div>
    `).join('')}
  </div>`;
}

async function viewHelpArticle(slug) {
  const res = await apiFetch(`/api/help/articles/${slug}`);
  if (!res || !res.article) return showToast('Article not found', 'error');
  const a = res.article;
  document.getElementById('page-content').innerHTML = `
    <div style="max-width:800px;margin:0 auto;padding:24px">
      <button class="btn btn-outline btn-sm" onclick="renderHelpCenter()" style="margin-bottom:16px">&larr; Back to Help Center</button>
      <span style="font-size:12px;background:#f3f4f6;padding:2px 8px;border-radius:4px;color:#666;margin-left:8px">${a.category}</span>
      <h1 style="font-size:24px;font-weight:700;margin:16px 0 8px">${a.title}</h1>
      <div style="font-size:15px;line-height:1.7;color:#333">${a.content}</div>
      ${a.related_page ? `<div style="margin-top:24px;padding:16px;background:#f9fafb;border-radius:8px"><strong>Related page:</strong> <a href="/${a.related_page}" style="color:#0ace0a;text-decoration:none">${a.related_page}</a></div>` : ''}
    </div>`;
}

async function searchHelp(query) {
  const url = query ? `/api/help/articles?q=${encodeURIComponent(query)}` : '/api/help/articles';
  const res = await apiFetch(url);
  const articles = (res || {}).articles || [];
  const list = document.getElementById('help-articles-list');
  if (list) list.innerHTML = renderHelpArticlesList(articles);
}

async function filterHelpCategory(cat) {
  window._helpCategoryFilter = cat;
  const url = cat ? `/api/help/articles?category=${encodeURIComponent(cat)}` : '/api/help/articles';
  const res = await apiFetch(url);
  const articles = (res || {}).articles || [];
  const list = document.getElementById('help-articles-list');
  if (list) list.innerHTML = renderHelpArticlesList(articles);
  renderHelpCenter();
}


// ==================== CYCLE 24: GLOBAL SEARCH ====================

async function renderGlobalSearch() {
  const savedRes = await apiFetch('/api/search/saved');
  const saved = (savedRes || {}).searches || [];

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:900px;margin:0 auto;padding:24px">
      <h1 style="font-size:24px;font-weight:700;margin-bottom:16px">Search</h1>
      <div style="display:flex;gap:8px;margin-bottom:24px">
        <input id="global-search-input" type="text" placeholder="Search interviews, candidates, help articles..." style="flex:1;padding:12px 16px;border:1px solid #e5e7eb;border-radius:8px;font-size:15px" onkeydown="if(event.key==='Enter')executeGlobalSearch()">
        <button class="btn btn-primary" onclick="executeGlobalSearch()">Search</button>
      </div>
      ${saved.length > 0 ? `<div style="margin-bottom:16px"><span style="font-size:13px;color:#666">Saved searches:</span> ${saved.map(s => `<button class="btn btn-sm btn-outline" style="margin:2px" onclick="document.getElementById('global-search-input').value='${s.query}';executeGlobalSearch()">${s.name}</button>`).join('')}</div>` : ''}
      <div id="search-results"></div>
    </div>`;
}

async function executeGlobalSearch() {
  const q = document.getElementById('global-search-input').value.trim();
  if (!q || q.length < 2) return showToast('Enter at least 2 characters', 'error');
  const res = await apiFetch(`/api/search/global?q=${encodeURIComponent(q)}`);
  if (!res) return;
  const container = document.getElementById('search-results');
  container.innerHTML = `
    <p style="font-size:14px;color:#666;margin-bottom:16px">${res.total_results} results for "${res.query}"</p>
    ${res.interviews.length ? `<h3 style="font-size:16px;font-weight:600;margin-bottom:8px">Interviews (${res.interviews.length})</h3>
      ${res.interviews.map(i => `<div class="card" style="padding:12px 16px;margin-bottom:8px;cursor:pointer" onclick="navigateTo('/interviews')">
        <div style="font-weight:600">${i.title}</div><div style="font-size:13px;color:#666">${i.position || ''} — ${i.status}</div>
      </div>`).join('')}` : ''}
    ${res.candidates.length ? `<h3 style="font-size:16px;font-weight:600;margin:16px 0 8px">Candidates (${res.candidates.length})</h3>
      ${res.candidates.map(c => `<div class="card" style="padding:12px 16px;margin-bottom:8px">
        <div style="font-weight:600">${c.name}</div><div style="font-size:13px;color:#666">${c.email} — ${c.interview_title || ''} — ${c.status}${c.ai_score ? ` (Score: ${c.ai_score})` : ''}</div>
      </div>`).join('')}` : ''}
    ${res.help_articles.length ? `<h3 style="font-size:16px;font-weight:600;margin:16px 0 8px">Help Articles (${res.help_articles.length})</h3>
      ${res.help_articles.map(a => `<div class="card" style="padding:12px 16px;margin-bottom:8px;cursor:pointer" onclick="navigateTo('/help-center')">
        <div style="font-weight:600">${a.title}</div><div style="font-size:13px;color:#666">${a.category}</div>
      </div>`).join('')}` : ''}
    ${res.total_results === 0 ? '<p style="text-align:center;color:#999;padding:32px">No results found</p>' : ''}
    <div style="margin-top:16px;text-align:right"><button class="btn btn-outline btn-sm" onclick="saveCurrentSearch()">Save this search</button></div>`;
}

async function saveCurrentSearch() {
  const q = document.getElementById('global-search-input').value.trim();
  if (!q) return;
  const name = prompt('Name this search:', q);
  if (!name) return;
  await apiFetch('/api/search/saved', {method:'POST', body:JSON.stringify({name, query:q})});
  showToast('Search saved', 'success');
}


// ==================== CYCLE 24: PROFILE & PREFERENCES ====================

async function renderProfileSettings() {
  const res = await apiFetch('/api/profile/me');
  if (!res) return;
  const { profile, preferences } = res;

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:900px;margin:0 auto;padding:24px">
      <h1 style="font-size:24px;font-weight:700;margin-bottom:24px">Profile & Preferences</h1>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px">
        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Profile</h3>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Full Name</label><input id="prof-name" type="text" value="${profile.name}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px"></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Agency Name</label><input id="prof-agency" type="text" value="${profile.agency_name}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px"></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Job Title</label><input id="prof-title" type="text" value="${profile.title || ''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="e.g. Hiring Manager"></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Phone</label><input id="prof-phone" type="text" value="${profile.phone || ''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="555-0100"></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Bio</label><textarea id="prof-bio" rows="3" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px" placeholder="Brief description...">${profile.bio || ''}</textarea></div>
          <button class="btn btn-primary" onclick="saveProfile()">Save Profile</button>
          <div style="margin-top:12px;padding:8px 12px;background:#f9fafb;border-radius:6px;font-size:13px;color:#666">
            <strong>Plan:</strong> ${profile.plan} &bull; <strong>Role:</strong> ${profile.role} &bull; <strong>Email:</strong> ${profile.email}
          </div>
        </div>

        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Preferences</h3>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Timezone</label><select id="pref-tz" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            ${['America/New_York','America/Chicago','America/Denver','America/Los_Angeles','America/Phoenix','Pacific/Honolulu'].map(tz => `<option value="${tz}" ${preferences.timezone===tz?'selected':''}>${tz.replace('_',' ')}</option>`).join('')}
          </select></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Date Format</label><select id="pref-date" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            <option value="MM/DD/YYYY" ${preferences.date_format==='MM/DD/YYYY'?'selected':''}>MM/DD/YYYY</option>
            <option value="DD/MM/YYYY" ${preferences.date_format==='DD/MM/YYYY'?'selected':''}>DD/MM/YYYY</option>
            <option value="YYYY-MM-DD" ${preferences.date_format==='YYYY-MM-DD'?'selected':''}>YYYY-MM-DD</option>
          </select></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Theme</label><select id="pref-theme" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            <option value="light" ${preferences.theme==='light'?'selected':''}>Light</option>
            <option value="dark" ${preferences.theme==='dark'?'selected':''}>Dark</option>
          </select></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Default Interview Duration (min)</label><input id="pref-duration" type="number" value="${preferences.default_interview_duration||30}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px"></div>
          <h4 style="font-size:14px;font-weight:600;margin:16px 0 8px">Notifications</h4>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:8px"><input type="checkbox" id="pref-email-notif" ${preferences.email_notifications?'checked':''}> Email notifications</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:8px"><input type="checkbox" id="pref-browser-notif" ${preferences.browser_notifications?'checked':''}> Browser notifications</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:8px"><input type="checkbox" id="pref-digest" ${preferences.weekly_digest?'checked':''}> Weekly digest email</label>
          <label style="display:flex;align-items:center;gap:8px;font-size:14px;margin-bottom:12px"><input type="checkbox" id="pref-cand-alerts" ${preferences.candidate_alerts?'checked':''}> Candidate completion alerts</label>
          <button class="btn btn-primary" onclick="savePreferences()">Save Preferences</button>
        </div>
      </div>
    </div>`;
}

async function saveProfile() {
  await apiFetch('/api/profile/me', {method:'PUT', body:JSON.stringify({
    name: document.getElementById('prof-name').value,
    agency_name: document.getElementById('prof-agency').value,
    title: document.getElementById('prof-title').value,
    phone: document.getElementById('prof-phone').value,
    bio: document.getElementById('prof-bio').value,
  })});
  showToast('Profile saved', 'success');
}

async function savePreferences() {
  await apiFetch('/api/profile/preferences', {method:'PUT', body:JSON.stringify({
    timezone: document.getElementById('pref-tz').value,
    date_format: document.getElementById('pref-date').value,
    theme: document.getElementById('pref-theme').value,
    default_interview_duration: parseInt(document.getElementById('pref-duration').value) || 30,
    email_notifications: document.getElementById('pref-email-notif').checked ? 1 : 0,
    browser_notifications: document.getElementById('pref-browser-notif').checked ? 1 : 0,
    weekly_digest: document.getElementById('pref-digest').checked ? 1 : 0,
    candidate_alerts: document.getElementById('pref-cand-alerts').checked ? 1 : 0,
  })});
  showToast('Preferences saved', 'success');
}


// ==================== CYCLE 24: DATA MANAGEMENT ====================

async function renderDataManagement() {
  const [exportsRes, importsRes] = await Promise.all([
    apiFetch('/api/data/exports'),
    apiFetch('/api/data/imports'),
  ]);
  const exports_list = (exportsRes || {}).exports || [];
  const imports_list = (importsRes || {}).imports || [];

  document.getElementById('page-content').innerHTML = `
    <div style="max-width:1000px;margin:0 auto;padding:24px">
      <h1 style="font-size:24px;font-weight:700;margin-bottom:24px">Data Management</h1>

      <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;margin-bottom:24px">
        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Export Data</h3>
          <p style="font-size:14px;color:#666;margin-bottom:16px">Download your data as CSV or JSON. Great for reporting, backups, or migrating to another system.</p>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Data Type</label><select id="export-type" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            <option value="candidates">Candidates</option><option value="interviews">Interviews</option><option value="email_log">Email Log</option><option value="analytics">Analytics</option><option value="all">Everything</option>
          </select></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Format</label><select id="export-format" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            <option value="csv">CSV</option><option value="json">JSON</option>
          </select></div>
          <button class="btn btn-primary" onclick="createExport()">Export Data</button>
        </div>

        <div class="card" style="padding:20px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Import Data</h3>
          <p style="font-size:14px;color:#666;margin-bottom:16px">Bulk import candidates from a spreadsheet or another system. Provide interview_id, name, and email.</p>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">Import Type</label><select id="import-type" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px">
            <option value="candidates">Candidates</option>
          </select></div>
          <div style="margin-bottom:12px"><label style="font-size:13px;color:#666;display:block;margin-bottom:4px">JSON Records</label><textarea id="import-data" rows="4" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-family:monospace;font-size:12px" placeholder='[{"name":"Jane Doe","email":"jane@agency.com","interview_id":"..."}]'></textarea></div>
          <button class="btn btn-primary" onclick="createImport()">Import Data</button>
        </div>
      </div>

      <div class="card" style="padding:20px;margin-bottom:16px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Export History</h3>
        ${exports_list.length === 0 ? '<p style="color:#999;font-size:14px">No exports yet</p>' :
          `<table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr style="border-bottom:2px solid #e5e7eb"><th style="text-align:left;padding:8px">Type</th><th style="text-align:left;padding:8px">Format</th><th style="text-align:left;padding:8px">Records</th><th style="text-align:left;padding:8px">Status</th><th style="text-align:left;padding:8px">Date</th></tr></thead><tbody>
          ${exports_list.map(e => `<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px">${e.export_type}</td><td style="padding:8px">${e.format}</td><td style="padding:8px">${e.record_count}</td><td style="padding:8px"><span style="color:#0ace0a;font-weight:600">${e.status}</span></td><td style="padding:8px;color:#666">${e.created_at ? new Date(e.created_at).toLocaleDateString() : ''}</td></tr>`).join('')}
          </tbody></table>`}
      </div>

      <div class="card" style="padding:20px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:12px">Import History</h3>
        ${imports_list.length === 0 ? '<p style="color:#999;font-size:14px">No imports yet</p>' :
          `<table style="width:100%;border-collapse:collapse;font-size:14px"><thead><tr style="border-bottom:2px solid #e5e7eb"><th style="text-align:left;padding:8px">Type</th><th style="text-align:left;padding:8px">Total</th><th style="text-align:left;padding:8px">Imported</th><th style="text-align:left;padding:8px">Skipped</th><th style="text-align:left;padding:8px">Date</th></tr></thead><tbody>
          ${imports_list.map(e => `<tr style="border-bottom:1px solid #f3f4f6"><td style="padding:8px">${e.import_type}</td><td style="padding:8px">${e.total_records}</td><td style="padding:8px;color:#0ace0a;font-weight:600">${e.imported_records}</td><td style="padding:8px;color:#999">${e.skipped_records}</td><td style="padding:8px;color:#666">${e.created_at ? new Date(e.created_at).toLocaleDateString() : ''}</td></tr>`).join('')}
          </tbody></table>`}
      </div>
    </div>`;
}

async function createExport() {
  const type = document.getElementById('export-type').value;
  const format = document.getElementById('export-format').value;
  const res = await apiFetch('/api/data/export', {method:'POST', body:JSON.stringify({type, format})});
  if (res && res.success) {
    showToast(`Exported ${res.record_count} records (${format.toUpperCase()})`, 'success');
    renderDataManagement();
  }
}

async function createImport() {
  const type = document.getElementById('import-type').value;
  let records;
  try { records = JSON.parse(document.getElementById('import-data').value); }
  catch(e) { return showToast('Invalid JSON format', 'error'); }
  if (!Array.isArray(records)) return showToast('Records must be an array', 'error');
  const res = await apiFetch('/api/data/import', {method:'POST', body:JSON.stringify({type, records})});
  if (res && res.success) {
    showToast(`Imported ${res.imported} of ${res.total} records`, 'success');
    renderDataManagement();
  }
}


// ==================== SECURITY SETTINGS (Cycle 25) ====================

async function renderSecuritySettings() {
  const [policy, history, events] = await Promise.all([
    apiFetch('/api/security/password-rules'),
    apiFetch('/api/security/login-history?limit=10'),
    apiFetch('/api/security/event-log?limit=10')
  ]);

  const pol = policy?.policy || {};
  const attempts = history?.attempts || [];
  const evts = events?.events || [];

  let loginRows = attempts.map(a => `<tr>
    <td>${new Date(a.created_at).toLocaleString()}</td>
    <td>${a.ip_address || '-'}</td>
    <td><span class="badge ${a.success ? 'badge-green' : 'badge-red'}">${a.success ? 'Success' : 'Failed'}</span></td>
    <td>${a.failure_reason || '-'}</td>
  </tr>`).join('') || '<tr><td colspan="4" style="text-align:center;color:#999">No login history</td></tr>';

  let eventRows = evts.map(e => `<tr>
    <td>${new Date(e.created_at).toLocaleString()}</td>
    <td><span class="badge badge-${e.severity === 'warning' ? 'yellow' : e.severity === 'critical' ? 'red' : 'gray'}">${e.event_type}</span></td>
    <td>${e.severity}</td>
    <td>${e.ip_address || '-'}</td>
  </tr>`).join('') || '<tr><td colspan="4" style="text-align:center;color:#999">No security events</td></tr>';

  document.getElementById('page-content').innerHTML = `
    <div class="page-header"><h1>Security Settings</h1><p>Password policy, login history, and security events</p></div>

    <div class="grid grid-2" style="gap:24px;margin-bottom:24px">
      <div class="card">
        <div class="card-header"><h3>Password Policy</h3></div>
        <div class="card-body">
          <div class="detail-row"><span>Minimum length</span><strong>${pol.min_length || 8} characters</strong></div>
          <div class="detail-row"><span>Uppercase required</span><strong>${pol.require_uppercase ? 'Yes' : 'No'}</strong></div>
          <div class="detail-row"><span>Lowercase required</span><strong>${pol.require_lowercase ? 'Yes' : 'No'}</strong></div>
          <div class="detail-row"><span>Digit required</span><strong>${pol.require_digit ? 'Yes' : 'No'}</strong></div>
          <div class="detail-row"><span>Lockout threshold</span><strong>${pol.lockout_threshold || 5} failed attempts</strong></div>
          <div class="detail-row"><span>Lockout duration</span><strong>${pol.lockout_duration_minutes || 15} minutes</strong></div>
        </div>
      </div>
      <div class="card">
        <div class="card-header"><h3>Change Password</h3></div>
        <div class="card-body">
          <div class="form-group"><label>Current Password</label><input type="password" id="current-pw" class="form-control" placeholder="Enter current password"></div>
          <div class="form-group"><label>New Password</label><input type="password" id="new-pw" class="form-control" placeholder="Min 8 chars, uppercase, lowercase, digit"></div>
          <button onclick="changePassword()" class="btn btn-primary">Change Password</button>
        </div>
      </div>
    </div>

    <div class="card" style="margin-bottom:24px">
      <div class="card-header"><h3>Login History</h3></div>
      <div class="card-body">
        <table class="data-table"><thead><tr><th>Time</th><th>IP Address</th><th>Status</th><th>Reason</th></tr></thead>
        <tbody>${loginRows}</tbody></table>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><h3>Security Events</h3></div>
      <div class="card-body">
        <table class="data-table"><thead><tr><th>Time</th><th>Event</th><th>Severity</th><th>IP</th></tr></thead>
        <tbody>${eventRows}</tbody></table>
      </div>
    </div>
  `;
}

async function changePassword() {
  const current = document.getElementById('current-pw').value;
  const newPw = document.getElementById('new-pw').value;
  if (!current || !newPw) return showToast('Both fields required', 'error');
  const res = await apiFetch('/api/security/update-password', {method:'POST', body:JSON.stringify({current_password: current, new_password: newPw})});
  if (res && res.success) {
    showToast('Password changed successfully', 'success');
    document.getElementById('current-pw').value = '';
    document.getElementById('new-pw').value = '';
  }
}


// ==================== ACTIVITY LOG (Cycle 25) ====================

async function renderActivityLog() {
  const [log, summary] = await Promise.all([
    apiFetch('/api/activity/audit-log?limit=25'),
    apiFetch('/api/activity/audit-summary')
  ]);

  const entries = log?.entries || [];
  const sum = summary || {};

  let rows = entries.map(e => `<tr>
    <td>${new Date(e.created_at).toLocaleString()}</td>
    <td><span class="badge badge-gray">${e.action || '-'}</span></td>
    <td>${e.resource_type || e.entity_type || '-'}</td>
    <td>${e.resource_name || e.resource_id || e.entity_id || '-'}</td>
    <td><span class="badge badge-${e.severity === 'warning' ? 'yellow' : e.severity === 'error' ? 'red' : 'gray'}">${e.severity || 'info'}</span></td>
    <td>${e.details || '-'}</td>
  </tr>`).join('') || '<tr><td colspan="6" style="text-align:center;color:#999">No activity recorded</td></tr>';

  let topActions = (sum.top_actions || []).map(a =>
    `<div style="display:flex;justify-content:space-between;padding:6px 0;border-bottom:1px solid #f3f4f6"><span>${a.action}</span><strong>${a.cnt}</strong></div>`
  ).join('') || '<div style="color:#999">No actions yet</div>';

  document.getElementById('page-content').innerHTML = `
    <div class="page-header">
      <h1>Activity Log</h1>
      <p>Track all user actions and system events</p>
      <div style="display:flex;gap:8px;margin-top:12px">
        <button onclick="exportActivity()" class="btn btn-outline">Export Log</button>
        <button onclick="logTestActivity()" class="btn btn-outline">Log Test Entry</button>
      </div>
    </div>

    <div class="grid grid-3" style="gap:16px;margin-bottom:24px">
      <div class="stat-card"><div class="stat-value">${sum.total_entries || 0}</div><div class="stat-label">Total Entries</div></div>
      <div class="stat-card"><div class="stat-value">${sum.recent_7d || 0}</div><div class="stat-label">Last 7 Days</div></div>
      <div class="stat-card"><div class="stat-value">${(sum.top_actions || []).length}</div><div class="stat-label">Action Types</div></div>
    </div>

    <div class="grid grid-2" style="gap:24px;margin-bottom:24px">
      <div class="card">
        <div class="card-header"><h3>Top Actions</h3></div>
        <div class="card-body">${topActions}</div>
      </div>
      <div class="card">
        <div class="card-header"><h3>Filters</h3></div>
        <div class="card-body">
          <div class="form-group"><label>Action</label><input type="text" id="filter-action" class="form-control" placeholder="e.g. login, create, update"></div>
          <div class="form-group"><label>Entity Type</label><input type="text" id="filter-entity" class="form-control" placeholder="e.g. interview, candidate"></div>
          <button onclick="filterActivity()" class="btn btn-primary">Apply Filters</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header"><h3>Activity Entries</h3></div>
      <div class="card-body">
        <table class="data-table"><thead><tr><th>Time</th><th>Action</th><th>Entity</th><th>Name/ID</th><th>Severity</th><th>Details</th></tr></thead>
        <tbody id="activity-rows">${rows}</tbody></table>
      </div>
    </div>
  `;
}

async function filterActivity() {
  const action = document.getElementById('filter-action').value;
  const entity = document.getElementById('filter-entity').value;
  let url = '/api/activity/audit-log?limit=25';
  if (action) url += `&action=${encodeURIComponent(action)}`;
  if (entity) url += `&entity_type=${encodeURIComponent(entity)}`;
  const log = await apiFetch(url);
  const entries = log?.entries || [];
  const rows = entries.map(e => `<tr>
    <td>${new Date(e.created_at).toLocaleString()}</td>
    <td><span class="badge badge-gray">${e.action || '-'}</span></td>
    <td>${e.resource_type || e.entity_type || '-'}</td>
    <td>${e.resource_name || e.resource_id || '-'}</td>
    <td><span class="badge badge-${e.severity === 'warning' ? 'yellow' : 'gray'}">${e.severity || 'info'}</span></td>
    <td>${e.details || '-'}</td>
  </tr>`).join('') || '<tr><td colspan="6" style="text-align:center;color:#999">No matching entries</td></tr>';
  document.getElementById('activity-rows').innerHTML = rows;
}

async function exportActivity() {
  const res = await apiFetch('/api/activity/audit-export');
  if (res && res.entries) {
    const blob = new Blob([JSON.stringify(res.entries, null, 2)], {type: 'application/json'});
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a'); a.href = url; a.download = 'activity_log.json'; a.click();
    showToast(`Exported ${res.count} entries`, 'success');
  }
}

async function logTestActivity() {
  const res = await apiFetch('/api/activity/audit-log', {method:'POST', body:JSON.stringify({action:'test_action', resource_type:'system', resource_name:'Manual test', details:'Test activity log entry', severity:'info'})});
  if (res && res.success) {
    showToast('Test activity logged', 'success');
    renderActivityLog();
  }
}


// ==================== CYCLE 32: CANDIDATE EXPERIENCE ====================

async function renderCandidateExperience() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}

  if (!interviews.length) {
    content.innerHTML = `<div style="max-width:600px;text-align:center;margin:80px auto">
      <div style="font-size:48px;margin-bottom:16px">&#x1F3AF;</div>
      <h2>No Interviews Yet</h2>
      <p style="color:#666">Create an interview first, then configure its candidate experience.</p>
    </div>`;
    return;
  }

  const selectedId = new URLSearchParams(window.location.search).get('interview_id') || interviews[0].id;
  let config = {};
  try { config = await api(`/api/interviews/${selectedId}/apply-config`); } catch(e) {}

  const fields = config.apply_fields || [];

  content.innerHTML = `
    <div style="max-width:800px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Candidate Portal</h2>
          <p style="color:#666;font-size:13px">Set up what candidates see when they apply, interview, and finish</p>
        </div>
        <select id="ce-interview-select" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
          onchange="history.replaceState(null,'','/candidate-experience?interview_id='+this.value);renderCandidateExperience()">
          ${interviews.map(i => `<option value="${i.id}" ${i.id===selectedId?'selected':''}>${i.title}</option>`).join('')}
        </select>
      </div>

      <!-- Public Apply Toggle -->
      <div class="card" style="padding:20px 24px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center">
        <div>
          <h3 style="font-size:15px;font-weight:600;margin-bottom:2px">Public Apply Page</h3>
          <p style="font-size:13px;color:#666">Allow candidates to apply directly at <code style="background:#f3f4f6;padding:2px 6px;border-radius:4px">/apply/${selectedId.substring(0,8)}...</code></p>
        </div>
        <label style="position:relative;width:44px;height:24px;cursor:pointer">
          <input type="checkbox" id="ce-public-enabled" ${config.public_apply_enabled ? 'checked' : ''} style="opacity:0;width:0;height:0">
          <div style="position:absolute;top:0;left:0;right:0;bottom:0;background:${config.public_apply_enabled?'#0ace0a':'#ccc'};border-radius:12px;transition:0.3s"></div>
          <div style="position:absolute;top:2px;left:2px;width:20px;height:20px;background:#fff;border-radius:50%;transition:0.3s;transform:${config.public_apply_enabled?'translateX(20px)':'translateX(0)'}"></div>
        </label>
      </div>

      <!-- Apply Page Config -->
      <div class="card" style="padding:24px;margin-bottom:16px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Apply Page Settings</h3>
        <div style="margin-bottom:16px">
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Apply Instructions</label>
          <textarea id="ce-apply-instructions" rows="3" placeholder="Additional context shown to applicants..."
            style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${config.apply_instructions||''}</textarea>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Estimated Duration (min)</label>
            <input type="number" id="ce-duration" value="${config.estimated_duration_min||15}" min="5" max="120"
              style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Show Progress Tracker</label>
            <select id="ce-show-progress" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="1" ${config.show_progress_tracker!==0?'selected':''}>Yes - candidates can track status</option>
              <option value="0" ${config.show_progress_tracker===0?'selected':''}>No - hide progress page</option>
            </select>
          </div>
        </div>
      </div>

      <!-- Interview Prep Config -->
      <div class="card" style="padding:24px;margin-bottom:16px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Interview Prep Page</h3>
        <div style="margin-bottom:16px">
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Prep Instructions</label>
          <textarea id="ce-prep-instructions" rows="3" placeholder="Tips and instructions shown before the interview starts..."
            style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${config.prep_instructions||''}</textarea>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Prep Video URL (optional)</label>
          <input type="url" id="ce-prep-video" value="${config.prep_video_url||''}" placeholder="https://youtube.com/embed/..."
            style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
      </div>

      <!-- Thank You Config -->
      <div class="card" style="padding:24px;margin-bottom:16px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Thank You / Next Steps</h3>
        <div style="margin-bottom:16px">
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Thank You Message</label>
          <textarea id="ce-thank-you" rows="2" placeholder="Message shown after interview completion..."
            style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${config.thank_you_message||''}</textarea>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Next Steps</label>
          <textarea id="ce-next-steps" rows="2" placeholder="What happens after submission, expected timeline..."
            style="width:100%;padding:10px 14px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;font-family:inherit;resize:vertical">${config.thank_you_next_steps||''}</textarea>
        </div>
      </div>

      <button class="btn btn-primary" onclick="c32SaveExperienceConfig('${selectedId}')" style="width:100%;padding:14px;font-size:15px;margin-bottom:16px">Save All Settings</button>

      <!-- Quick Links -->
      <div class="card" style="padding:20px 24px;background:#f9fafb">
        <h3 style="font-size:14px;font-weight:600;margin-bottom:12px;color:#666">Preview Links</h3>
        <div style="display:flex;gap:12px;flex-wrap:wrap">
          <a href="/apply/${selectedId}" target="_blank" style="font-size:13px;color:#0ace0a;text-decoration:none;font-weight:600">Apply Page &#x2197;</a>
          <span style="color:#ddd">|</span>
          <a href="/job-board?interview_id=${selectedId}" style="font-size:13px;color:#0ace0a;text-decoration:none;font-weight:600">Job Board Settings</a>
        </div>
      </div>
    </div>`;

  // Wire up toggle
  const toggle = document.getElementById('ce-public-enabled');
  if (toggle) {
    toggle.addEventListener('change', function() {
      this.parentElement.querySelector('div:nth-child(2)').style.background = this.checked ? '#0ace0a' : '#ccc';
      this.parentElement.querySelector('div:nth-child(3)').style.transform = this.checked ? 'translateX(20px)' : 'translateX(0)';
    });
  }
}

async function c32SaveExperienceConfig(interviewId) {
  try {
    await api(`/api/interviews/${interviewId}/apply-config`, {
      method: 'PUT',
      body: JSON.stringify({
        public_apply_enabled: document.getElementById('ce-public-enabled').checked,
        apply_instructions: document.getElementById('ce-apply-instructions').value,
        estimated_duration_min: parseInt(document.getElementById('ce-duration').value) || 15,
        show_progress_tracker: document.getElementById('ce-show-progress').value === '1',
        prep_instructions: document.getElementById('ce-prep-instructions').value,
        prep_video_url: document.getElementById('ce-prep-video').value,
        thank_you_message: document.getElementById('ce-thank-you').value,
        thank_you_next_steps: document.getElementById('ce-next-steps').value,
      })
    });
    toast('Candidate experience settings saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
}


// ==================== CYCLE 31: JOB BOARDS & PIPELINE POWER-UP ====================

// --- C31 Feature 1: Pipeline Funnel Analytics ---

async function renderPipelineFunnel() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading pipeline analytics...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  const interviewId = new URLSearchParams(window.location.search).get('interview_id') || '';

  const params = interviewId ? `?interview_id=${interviewId}` : '';
  const [funnelData, sourceData] = await Promise.all([
    api(`/api/analytics/pipeline-funnel${params}`),
    api(`/api/analytics/sources${params}`)
  ]);

  const funnel = funnelData.funnel || [];
  const maxCount = Math.max(...funnel.map(f => f.count), 1);
  const stageColors = { new:'#6b7280', in_review:'#3b82f6', shortlisted:'#8b5cf6',
    interview_scheduled:'#f59e0b', offered:'#10b981', hired:'#059669' };

  content.innerHTML = `
    <div style="max-width:1100px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Hiring Funnel</h2>
          <p style="color:#666;font-size:13px">${funnelData.total_candidates || 0} total candidates &middot; ${funnelData.hire_rate || 0}% hire rate</p>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="funnel-filter" class="form-input" style="width:auto;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
            onchange="const v=this.value;history.replaceState(null,'',v?'/pipeline-funnel?interview_id='+v:'/pipeline-funnel');renderPipelineFunnel()">
            <option value="">All Interviews</option>
            ${(interviews||[]).map(i => `<option value="${i.id}" ${i.id===interviewId?'selected':''}>${i.title}</option>`).join('')}
          </select>
        </div>
      </div>

      <!-- Funnel Visualization -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:28px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:20px">Hiring Funnel</h3>
        <div style="display:flex;flex-direction:column;gap:2px">
          ${funnel.map((s, i) => {
            const width = Math.max(s.count / maxCount * 100, 12);
            const color = stageColors[s.stage] || '#6b7280';
            return `<div style="display:flex;align-items:center;gap:16px">
              <div style="width:140px;text-align:right;font-size:13px;font-weight:600;color:${color}">${s.label}</div>
              <div style="flex:1;position:relative">
                <div style="height:40px;background:${color}18;border-radius:6px;overflow:hidden;position:relative">
                  <div style="height:100%;width:${width}%;background:${color};border-radius:6px;display:flex;align-items:center;padding-left:12px;min-width:60px;transition:width 0.5s">
                    <span style="color:#fff;font-weight:700;font-size:14px;text-shadow:0 1px 2px rgba(0,0,0,0.2)">${s.count}</span>
                  </div>
                </div>
              </div>
              <div style="width:80px;text-align:right">
                ${i > 0 ? `<span style="font-size:12px;color:${s.conversion_from_prev >= 50 ? '#059669' : s.conversion_from_prev >= 25 ? '#d97706' : '#dc2626'};font-weight:600">${s.conversion_from_prev}%</span>` : '<span style="font-size:12px;color:#999">—</span>'}
              </div>
            </div>`;
          }).join('')}
          ${funnelData.rejected ? `<div style="display:flex;align-items:center;gap:16px;margin-top:8px;opacity:0.6">
            <div style="width:140px;text-align:right;font-size:13px;font-weight:600;color:#ef4444">Rejected</div>
            <div style="flex:1"><div style="height:28px;background:#ef444418;border-radius:6px;overflow:hidden">
              <div style="height:100%;width:${Math.max(funnelData.rejected/maxCount*100,8)}%;background:#ef4444;border-radius:6px;display:flex;align-items:center;padding-left:12px">
                <span style="color:#fff;font-weight:700;font-size:13px">${funnelData.rejected}</span>
              </div></div></div>
            <div style="width:80px"></div>
          </div>` : ''}
        </div>
      </div>

      <!-- Time in Stage + Source Breakdown -->
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:24px">
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Avg. Time in Stage</h3>
          ${Object.keys(funnelData.time_in_stage || {}).length ? Object.entries(funnelData.time_in_stage).map(([stage, days]) => {
            const color = days > 14 ? '#dc2626' : days > 7 ? '#d97706' : '#059669';
            return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f3f4f6">
              <span style="font-size:13px;text-transform:capitalize">${stage.replace(/_/g,' ')}</span>
              <span style="font-size:13px;font-weight:700;color:${color}">${days} days</span>
            </div>`;
          }).join('') : '<p style="color:#999;font-size:13px;text-align:center;padding:20px 0">No stage timing data yet</p>'}
        </div>

        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
          <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Candidate Sources</h3>
          ${(sourceData.sources || []).length ? (sourceData.sources || []).slice(0,6).map(s => {
            return `<div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0;border-bottom:1px solid #f3f4f6">
              <div>
                <span style="font-size:13px;font-weight:500;text-transform:capitalize">${s.src.replace(/_/g,' ')}</span>
                <span style="font-size:11px;color:#999;margin-left:6px">${s.pct}%</span>
              </div>
              <div style="display:flex;align-items:center;gap:8px">
                <div style="width:60px;height:6px;background:#e5e7eb;border-radius:3px;overflow:hidden">
                  <div style="height:100%;width:${s.pct}%;background:#0ace0a;border-radius:3px"></div>
                </div>
                <span style="font-size:13px;font-weight:600;min-width:30px;text-align:right">${s.cnt}</span>
              </div>
            </div>`;
          }).join('') : '<p style="color:#999;font-size:13px;text-align:center;padding:20px 0">No source data yet</p>'}
        </div>
      </div>
    </div>`;
}


// --- C31 Feature 2: Enhanced Kanban Board ---

let c31KanbanSearch = '';
let c31SelectedCandidates = new Set();

async function renderEnhancedKanban() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading pipeline...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  const interviewId = new URLSearchParams(window.location.search).get('interview_id') || '';

  const params = new URLSearchParams();
  if (interviewId) params.set('interview_id', interviewId);
  if (c31KanbanSearch) params.set('q', c31KanbanSearch);
  const qStr = params.toString() ? '?' + params.toString() : '';

  const data = await api(`/api/candidates/kanban-enhanced${qStr}`);
  const stages = data.stages || [];
  c31SelectedCandidates.clear();

  content.innerHTML = `
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
      <div>
        <h1 style="font-size:24px;font-weight:700">Hiring Board</h1>
        <p style="color:#666;margin-top:4px;font-size:13px">${data.total || 0} candidates across ${stages.length} stages</p>
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input type="text" id="c31-kanban-search" placeholder="Search candidates..." value="${c31KanbanSearch}"
          style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;width:200px"
          onkeydown="if(event.key==='Enter'){c31KanbanSearch=this.value;renderEnhancedKanban()}">
        <select class="form-input" style="width:auto;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
          onchange="const v=this.value;history.replaceState(null,'',v?'/enhanced-kanban?interview_id='+v:'/enhanced-kanban');renderEnhancedKanban()">
          <option value="">All Interviews</option>
          ${(interviews||[]).map(i => `<option value="${i.id}" ${i.id===interviewId?'selected':''}>${i.title}</option>`).join('')}
        </select>
        <button class="btn btn-sm btn-outline" onclick="renderPipelineFunnel()" title="View funnel analytics">📊 Funnel</button>
        <button class="btn btn-sm btn-outline" onclick="renderAutoRules()" title="Configure auto-stage rules">⚡ Rules</button>
      </div>
    </div>

    <!-- Bulk Action Bar -->
    <div id="c31-bulk-bar" style="display:none;background:#111;color:#fff;padding:10px 16px;border-radius:8px;margin-bottom:12px;align-items:center;gap:12px">
      <span id="c31-bulk-count">0 selected</span>
      <select id="c31-bulk-stage" style="padding:6px 10px;border-radius:6px;border:1px solid #333;background:#222;color:#fff;font-size:13px">
        <option value="">Move to stage...</option>
        ${stages.map(s => `<option value="${s.slug}">${s.name}</option>`).join('')}
      </select>
      <button class="btn btn-sm btn-primary" onclick="c31BulkMoveStage()" style="font-size:12px">Move</button>
      <input type="text" id="c31-bulk-tag" placeholder="Add tag..." style="padding:6px 10px;border-radius:6px;border:1px solid #333;background:#222;color:#fff;font-size:13px;width:120px">
      <button class="btn btn-sm" onclick="c31BulkTag()" style="font-size:12px;background:#8b5cf6;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer">Tag</button>
      <button class="btn btn-sm" onclick="c31BulkArchive()" style="font-size:12px;background:#dc2626;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer">Archive</button>
      <button class="btn btn-sm btn-outline" onclick="c31SelectedCandidates.clear();c31UpdateBulkBar();renderEnhancedKanban()" style="font-size:12px;color:#fff;border-color:#555">Clear</button>
    </div>

    <!-- Kanban Columns -->
    <div style="display:flex;gap:12px;overflow-x:auto;padding-bottom:16px;min-height:500px">
      ${stages.map(stage => `
        <div class="kanban-column" data-stage="${stage.slug}" style="min-width:230px;max-width:270px;flex:1;background:#f9fafb;border-radius:12px;padding:12px"
          ondragover="event.preventDefault();this.style.background='#e6fce6'" ondragleave="this.style.background='#f9fafb'"
          ondrop="c31DropCandidate(event,'${stage.slug}');this.style.background='#f9fafb'">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;padding-bottom:8px;border-bottom:3px solid ${stage.color}">
            <span style="font-weight:700;font-size:13px;color:${stage.color}">${stage.name}</span>
            <span style="background:${stage.color}22;color:${stage.color};font-size:12px;padding:2px 8px;border-radius:10px;font-weight:600">${stage.count}</span>
          </div>
          ${(stage.candidates || []).map(c => `
            <div draggable="true" ondragstart="event.dataTransfer.setData('text/plain','${c.id}')"
              style="background:white;border-radius:8px;padding:10px 12px;margin-bottom:8px;cursor:grab;box-shadow:0 1px 3px rgba(0,0,0,0.08);border-left:3px solid ${stage.color};position:relative"
              onclick="if(event.target.type!=='checkbox')window.location.href='/review/${c.id}'">
              <input type="checkbox" style="position:absolute;top:10px;right:10px;cursor:pointer"
                ${c31SelectedCandidates.has(c.id)?'checked':''}
                onchange="event.stopPropagation();if(this.checked)c31SelectedCandidates.add('${c.id}');else c31SelectedCandidates.delete('${c.id}');c31UpdateBulkBar()">
              <div style="font-weight:600;font-size:14px;padding-right:24px">${c.first_name} ${c.last_name}</div>
              <div style="font-size:12px;color:#666;margin-top:2px">${c.interview_title || ''}</div>
              <div style="display:flex;justify-content:space-between;align-items:center;margin-top:6px">
                ${c.ai_score ? `<span style="font-size:12px;color:#0ace0a;font-weight:600">Score: ${c.ai_score}</span>` : '<span></span>'}
                ${c.days_in_stage > 7 ? `<span style="font-size:11px;color:#d97706;font-weight:500">${c.days_in_stage}d</span>` : ''}
              </div>
              ${c.source && c.source !== 'manual' ? `<div style="font-size:10px;color:#999;margin-top:4px;text-transform:capitalize">via ${c.source.replace(/_/g,' ')}</div>` : ''}
            </div>
          `).join('')}
          ${(stage.candidates || []).length === 0 ? '<p style="text-align:center;color:#ccc;font-size:13px;margin-top:20px">No candidates</p>' : ''}
        </div>
      `).join('')}
    </div>`;
}

async function c31DropCandidate(event, newStage) {
  event.preventDefault();
  const candidateId = event.dataTransfer.getData('text/plain');
  if (!candidateId) return;
  try {
    await api('/api/candidates/' + candidateId + '/pipeline-stage', {
      method: 'PUT', body: JSON.stringify({ stage: newStage, order: 0 })
    });
    toast('Moved to ' + newStage.replace(/_/g, ' '), 'success');
    renderEnhancedKanban();
  } catch(e) { toast(e.message, 'error'); }
}

function c31UpdateBulkBar() {
  const bar = document.getElementById('c31-bulk-bar');
  if (!bar) return;
  const count = c31SelectedCandidates.size;
  bar.style.display = count > 0 ? 'flex' : 'none';
  const el = document.getElementById('c31-bulk-count');
  if (el) el.textContent = `${count} selected`;
}

async function c31BulkMoveStage() {
  const stage = document.getElementById('c31-bulk-stage').value;
  if (!stage) { toast('Select a stage first', 'error'); return; }
  if (c31SelectedCandidates.size === 0) return;
  try {
    await api('/api/candidates/bulk-action', {
      method: 'POST', body: JSON.stringify({ candidate_ids: [...c31SelectedCandidates], action: 'move_stage', value: stage })
    });
    toast(`Moved ${c31SelectedCandidates.size} candidates to ${stage.replace(/_/g,' ')}`, 'success');
    c31SelectedCandidates.clear();
    renderEnhancedKanban();
  } catch(e) { toast(e.message, 'error'); }
}

async function c31BulkTag() {
  const tag = (document.getElementById('c31-bulk-tag').value || '').trim();
  if (!tag) { toast('Enter a tag first', 'error'); return; }
  if (c31SelectedCandidates.size === 0) return;
  try {
    await api('/api/candidates/bulk-action', {
      method: 'POST', body: JSON.stringify({ candidate_ids: [...c31SelectedCandidates], action: 'tag', value: tag })
    });
    toast(`Tagged ${c31SelectedCandidates.size} candidates as "${tag}"`, 'success');
    c31SelectedCandidates.clear();
    renderEnhancedKanban();
  } catch(e) { toast(e.message, 'error'); }
}

async function c31BulkArchive() {
  if (c31SelectedCandidates.size === 0) return;
  if (!confirm(`Archive ${c31SelectedCandidates.size} candidate(s)?`)) return;
  try {
    await api('/api/candidates/bulk-action', {
      method: 'POST', body: JSON.stringify({ candidate_ids: [...c31SelectedCandidates], action: 'archive' })
    });
    toast(`Archived ${c31SelectedCandidates.size} candidates`, 'success');
    c31SelectedCandidates.clear();
    renderEnhancedKanban();
  } catch(e) { toast(e.message, 'error'); }
}


// --- C31 Feature 3: Auto-Stage Rules Config ---

async function renderAutoRules() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading rules...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  const data = await api('/api/auto-rules');
  const rules = data.rules || [];

  const ruleTypeLabels = {
    ai_score_gte: 'AI Score ≥', ai_score_lt: 'AI Score <',
    days_inactive: 'Days Inactive ≥', interview_completed: 'Interview Completed'
  };
  const stageLabels = {
    new: 'New', in_review: 'In Review', shortlisted: 'Shortlisted',
    interview_scheduled: 'Scheduled', offered: 'Offered', hired: 'Hired', rejected: 'Rejected'
  };

  content.innerHTML = `
    <div style="max-width:800px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Auto-Rules</h2>
          <p style="color:#666;font-size:13px">Automatically move candidates along when certain things happen</p>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm btn-primary" onclick="c31ApplyRules()">⚡ Run Rules Now</button>
          <button class="btn btn-sm btn-outline" onclick="renderEnhancedKanban()">← Back to Hiring Board</button>
        </div>
      </div>

      <!-- Create Rule Form -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Create New Rule</h3>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Interview</label>
            <select id="rule-interview" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="">All Interviews</option>
              ${(interviews||[]).map(i => `<option value="${i.id}">${i.title}</option>`).join('')}
            </select>
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Rule Type</label>
            <select id="rule-type" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
              onchange="document.getElementById('rule-trigger-row').style.display=this.value==='interview_completed'?'none':'block'">
              <option value="ai_score_gte">AI Score ≥ threshold</option>
              <option value="ai_score_lt">AI Score &lt; threshold</option>
              <option value="days_inactive">Days inactive ≥</option>
              <option value="interview_completed">Interview completed</option>
            </select>
          </div>
          <div id="rule-trigger-row">
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Trigger Value</label>
            <input type="text" id="rule-trigger" placeholder="e.g. 80, 14" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">From Stage (optional)</label>
            <select id="rule-from" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="">Any stage</option>
              ${Object.entries(stageLabels).map(([k,v]) => `<option value="${k}">${v}</option>`).join('')}
            </select>
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Move To Stage</label>
            <select id="rule-to" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              ${Object.entries(stageLabels).map(([k,v]) => `<option value="${k}">${v}</option>`).join('')}
            </select>
          </div>
          <div style="display:flex;align-items:end">
            <button class="btn btn-primary" onclick="c31CreateRule()" style="width:100%">+ Add Rule</button>
          </div>
        </div>
      </div>

      <!-- Existing Rules -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Active Rules (${rules.length})</h3>
        ${rules.length ? rules.map(r => {
          const typeLbl = ruleTypeLabels[r.rule_type] || r.rule_type;
          const fromLbl = r.from_stage ? (stageLabels[r.from_stage] || r.from_stage) : 'Any';
          const toLbl = stageLabels[r.to_stage] || r.to_stage;
          const iName = (interviews||[]).find(i => i.id === r.interview_id);
          return `<div style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;border-bottom:1px solid #f3f4f6">
            <div>
              <div style="font-size:14px;font-weight:500">
                <span style="color:#8b5cf6">⚡ ${typeLbl}</span> ${r.trigger_value || ''}
                <span style="color:#999;margin:0 4px">→</span>
                <span style="color:#059669">Move to ${toLbl}</span>
              </div>
              <div style="font-size:12px;color:#999;margin-top:2px">
                From: ${fromLbl} &middot; Interview: ${iName ? iName.title : 'All'}
              </div>
            </div>
            <button class="btn btn-sm" onclick="c31DeleteRule('${r.id}')" style="color:#dc2626;background:none;border:1px solid #fca5a5;padding:4px 12px;border-radius:6px;font-size:12px;cursor:pointer">Delete</button>
          </div>`;
        }).join('') : '<p style="color:#999;font-size:13px;text-align:center;padding:20px 0">No rules configured yet. Create one above to automate your pipeline.</p>'}
      </div>
    </div>`;
}

async function c31CreateRule() {
  const interview_id = document.getElementById('rule-interview').value;
  const rule_type = document.getElementById('rule-type').value;
  const trigger_value = document.getElementById('rule-trigger').value;
  const from_stage = document.getElementById('rule-from').value;
  const to_stage = document.getElementById('rule-to').value;

  try {
    await api('/api/auto-rules', {
      method: 'POST',
      body: JSON.stringify({ interview_id, rule_type, trigger_value, from_stage, to_stage })
    });
    toast('Rule created', 'success');
    renderAutoRules();
  } catch(e) { toast(e.message, 'error'); }
}

async function c31DeleteRule(ruleId) {
  if (!confirm('Delete this rule?')) return;
  try {
    await api(`/api/auto-rules/${ruleId}`, { method: 'DELETE' });
    toast('Rule deleted', 'success');
    renderAutoRules();
  } catch(e) { toast(e.message, 'error'); }
}

async function c31ApplyRules() {
  try {
    const res = await api('/api/auto-rules/apply', { method: 'POST', body: JSON.stringify({}) });
    toast(`Rules applied: ${res.candidates_moved} candidates moved (${res.rules_evaluated} rules evaluated)`, 'success');
  } catch(e) { toast(e.message, 'error'); }
}


// --- C31 Feature 4: Custom Pipeline Stages ---

async function renderCustomStages() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading stage config...</div>';

  let interviews = [];
  try { interviews = (await api('/api/interviews')).filter(i => i.status === 'active'); } catch(e) {
    try { interviews = await api('/api/interviews'); } catch(e2) { interviews = []; }
  }

  if (!interviews.length) {
    content.innerHTML = `<div style="max-width:600px;text-align:center;margin:80px auto">
      <div style="font-size:48px;margin-bottom:16px">📋</div>
      <h2>No Active Interviews</h2>
      <p style="color:#666">Create an interview first, then customize its pipeline stages.</p>
      <button class="btn btn-primary" onclick="APP_PAGE='interview_builder';loadPage()" style="margin-top:16px">Create Interview</button>
    </div>`;
    return;
  }

  const selectedId = new URLSearchParams(window.location.search).get('interview_id') || interviews[0].id;
  const stagesData = await api(`/api/interviews/${selectedId}/stages`);
  const stages = stagesData.stages || [];
  const isCustom = stagesData.custom_enabled;
  const defaultColors = ['#6b7280','#3b82f6','#8b5cf6','#f59e0b','#10b981','#059669','#ef4444','#ec4899','#f97316','#06b6d4'];

  content.innerHTML = `
    <div style="max-width:800px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Hiring Stages</h2>
          <p style="color:#666;font-size:13px">Set up the steps each candidate goes through in your hiring process</p>
        </div>
        <div style="display:flex;gap:8px">
          <select id="stage-interview-select" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
            onchange="history.replaceState(null,'','/custom-stages?interview_id='+this.value);renderCustomStages()">
            ${interviews.map(i => `<option value="${i.id}" ${i.id===selectedId?'selected':''}>${i.title}</option>`).join('')}
          </select>
          <button class="btn btn-sm btn-outline" onclick="renderEnhancedKanban()">← Hiring Board</button>
        </div>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:16px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:16px">
          <h3 style="font-size:16px;font-weight:600">
            ${isCustom ? '<span style="color:#0ace0a">Custom Stages</span>' : 'Default Stages'}
          </h3>
          <div style="display:flex;gap:8px">
            ${isCustom ? `<button class="btn btn-sm" onclick="c31ResetStages('${selectedId}')" style="color:#dc2626;background:none;border:1px solid #fca5a5;padding:6px 12px;border-radius:6px;font-size:12px;cursor:pointer">Reset to Default</button>` : ''}
            <button class="btn btn-sm btn-primary" onclick="c31SaveCustomStages('${selectedId}')">Save Changes</button>
          </div>
        </div>

        <div id="c31-stages-list">
          ${stages.map((s, i) => `
            <div class="c31-stage-row" style="display:flex;gap:8px;align-items:center;margin-bottom:8px;padding:8px 12px;background:#f9fafb;border-radius:8px" data-idx="${i}">
              <span style="color:#999;font-size:12px;cursor:grab;user-select:none">☰</span>
              <input type="color" value="${s.color || defaultColors[i % defaultColors.length]}" style="width:32px;height:32px;border:none;cursor:pointer" class="stage-color">
              <input type="text" value="${s.name}" placeholder="Stage name" class="stage-name"
                style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
              <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#666;cursor:pointer">
                <input type="checkbox" class="stage-terminal" ${s.is_terminal ? 'checked' : ''}> Terminal
              </label>
              <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#666;cursor:pointer">
                <input type="checkbox" class="stage-notes" ${s.require_notes ? 'checked' : ''}> Require Notes
              </label>
              <button onclick="this.closest('.c31-stage-row').remove()" style="background:none;border:none;color:#dc2626;cursor:pointer;font-size:16px;padding:4px">&times;</button>
            </div>
          `).join('')}
        </div>

        <button onclick="c31AddStageRow()" class="btn btn-sm btn-outline" style="margin-top:8px;width:100%;border-style:dashed">+ Add Stage</button>
      </div>
    </div>`;
}

function c31AddStageRow() {
  const list = document.getElementById('c31-stages-list');
  const idx = list.children.length;
  const colors = ['#6b7280','#3b82f6','#8b5cf6','#f59e0b','#10b981','#059669','#ef4444','#ec4899'];
  const html = `<div class="c31-stage-row" style="display:flex;gap:8px;align-items:center;margin-bottom:8px;padding:8px 12px;background:#f9fafb;border-radius:8px" data-idx="${idx}">
    <span style="color:#999;font-size:12px;cursor:grab;user-select:none">☰</span>
    <input type="color" value="${colors[idx % colors.length]}" style="width:32px;height:32px;border:none;cursor:pointer" class="stage-color">
    <input type="text" value="" placeholder="Stage name" class="stage-name" style="flex:1;padding:8px 12px;border:1px solid #e5e7eb;border-radius:6px;font-size:13px">
    <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#666;cursor:pointer"><input type="checkbox" class="stage-terminal"> Terminal</label>
    <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:#666;cursor:pointer"><input type="checkbox" class="stage-notes"> Require Notes</label>
    <button onclick="this.closest('.c31-stage-row').remove()" style="background:none;border:none;color:#dc2626;cursor:pointer;font-size:16px;padding:4px">&times;</button>
  </div>`;
  list.insertAdjacentHTML('beforeend', html);
}

async function c31SaveCustomStages(interviewId) {
  const rows = document.querySelectorAll('.c31-stage-row');
  const stages = [];
  rows.forEach(row => {
    const name = row.querySelector('.stage-name').value.trim();
    if (!name) return;
    stages.push({
      name, slug: name.toLowerCase().replace(/\s+/g, '_').replace(/[^a-z0-9_]/g, ''),
      color: row.querySelector('.stage-color').value,
      is_terminal: row.querySelector('.stage-terminal').checked,
      require_notes: row.querySelector('.stage-notes').checked
    });
  });
  if (stages.length < 2) { toast('Need at least 2 stages', 'error'); return; }
  try {
    await api(`/api/interviews/${interviewId}/stages`, {
      method: 'PUT', body: JSON.stringify({ stages })
    });
    toast(`Saved ${stages.length} custom stages`, 'success');
    renderCustomStages();
  } catch(e) { toast(e.message, 'error'); }
}

async function c31ResetStages(interviewId) {
  if (!confirm('Reset to default stages? Custom stages will be removed.')) return;
  try {
    await api(`/api/interviews/${interviewId}/stages/reset`, { method: 'POST' });
    toast('Reset to default stages', 'success');
    renderCustomStages();
  } catch(e) { toast(e.message, 'error'); }
}


// --- C31 Feature 5: Source Tracking Analytics ---

async function renderSourceTracking() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading source analytics...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  const interviewId = new URLSearchParams(window.location.search).get('interview_id') || '';
  const params = interviewId ? `?interview_id=${interviewId}` : '';

  const data = await api(`/api/analytics/sources${params}`);
  const sources = data.sources || [];
  const maxCnt = Math.max(...sources.map(s => s.cnt), 1);

  content.innerHTML = `
    <div style="max-width:900px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Where They Found You</h2>
          <p style="color:#666;font-size:13px">${data.total || 0} candidates from ${sources.length} sources</p>
        </div>
        <select style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
          onchange="const v=this.value;history.replaceState(null,'',v?'/source-tracking?interview_id='+v:'/source-tracking');renderSourceTracking()">
          <option value="">All Interviews</option>
          ${(interviews||[]).map(i => `<option value="${i.id}" ${i.id===interviewId?'selected':''}>${i.title}</option>`).join('')}
        </select>
      </div>

      <!-- Source Bar Chart -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:20px">Candidates by Source</h3>
        ${sources.length ? sources.map(s => {
          const barW = Math.max(s.cnt / maxCnt * 100, 4);
          return `<div style="display:flex;align-items:center;gap:12px;margin-bottom:12px">
            <div style="width:120px;text-align:right;font-size:13px;font-weight:500;text-transform:capitalize">${s.src.replace(/_/g,' ')}</div>
            <div style="flex:1;height:28px;background:#f3f4f6;border-radius:6px;overflow:hidden">
              <div style="height:100%;width:${barW}%;background:linear-gradient(90deg,#0ace0a,#059669);border-radius:6px;display:flex;align-items:center;padding-left:8px">
                <span style="color:#fff;font-weight:700;font-size:12px;text-shadow:0 1px 2px rgba(0,0,0,0.2)">${s.cnt}</span>
              </div>
            </div>
            <div style="width:60px;text-align:right;font-size:12px;color:#666">${s.pct}%</div>
          </div>`;
        }).join('') : '<p style="color:#999;text-align:center;padding:20px">No source data available yet</p>'}
      </div>

      <!-- Source Performance Table -->
      ${sources.length ? `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:0;overflow:hidden">
        <table>
          <thead><tr>
            <th style="padding:12px 16px;text-align:left">Source</th>
            <th style="padding:12px 16px;text-align:right">Candidates</th>
            <th style="padding:12px 16px;text-align:right">% of Total</th>
            <th style="padding:12px 16px;text-align:right">Hire Rate</th>
            <th style="padding:12px 16px;text-align:right">Avg AI Score</th>
          </tr></thead>
          <tbody>
            ${sources.map(s => `<tr>
              <td style="padding:10px 16px;text-transform:capitalize;font-weight:500">${s.src.replace(/_/g,' ')}</td>
              <td style="padding:10px 16px;text-align:right;font-weight:600">${s.cnt}</td>
              <td style="padding:10px 16px;text-align:right">${s.pct}%</td>
              <td style="padding:10px 16px;text-align:right;color:${s.hire_rate >= 20 ? '#059669' : s.hire_rate > 0 ? '#d97706' : '#999'};font-weight:600">${s.hire_rate}%</td>
              <td style="padding:10px 16px;text-align:right;color:${s.avg_score >= 70 ? '#059669' : s.avg_score >= 50 ? '#d97706' : '#999'}">${s.avg_score || '—'}</td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>` : ''}
    </div>`;
}


// --- C31 Feature 6: Job Board Settings (in Interview Detail) ---

async function renderJobBoardSettings() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading job board settings...</div>';

  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}

  if (!interviews.length) {
    content.innerHTML = `<div style="max-width:600px;text-align:center;margin:80px auto">
      <div style="font-size:48px;margin-bottom:16px">📋</div>
      <h2>No Interviews</h2>
      <p style="color:#666">Create an interview first to configure its job board listing.</p>
    </div>`;
    return;
  }

  const selectedId = new URLSearchParams(window.location.search).get('interview_id') || interviews[0].id;
  const interview = interviews.find(i => i.id === selectedId) || interviews[0];

  content.innerHTML = `
    <div style="max-width:700px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Job Board</h2>
          <p style="color:#666;font-size:13px">Publish open positions to your public job board</p>
        </div>
        <select style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
          onchange="history.replaceState(null,'','/job-board?interview_id='+this.value);renderJobBoardSettings()">
          ${interviews.map(i => `<option value="${i.id}" ${i.id===selectedId?'selected':''}>${i.title}</option>`).join('')}
        </select>
      </div>

      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <div>
            <h3 style="font-size:16px;font-weight:600">Publish to Job Board</h3>
            <p style="font-size:13px;color:#666">When enabled, this position appears on your public careers page</p>
          </div>
          <label style="position:relative;width:44px;height:24px;cursor:pointer">
            <input type="checkbox" id="jb-enabled" ${interview.job_board_enabled ? 'checked' : ''} style="opacity:0;width:0;height:0"
              onchange="document.getElementById('jb-toggle-dot').style.transform=this.checked?'translateX(20px)':'translateX(0)';document.getElementById('jb-toggle-bg').style.background=this.checked?'#0ace0a':'#ccc'">
            <div id="jb-toggle-bg" style="position:absolute;top:0;left:0;right:0;bottom:0;background:${interview.job_board_enabled?'#0ace0a':'#ccc'};border-radius:12px;transition:0.3s"></div>
            <div id="jb-toggle-dot" style="position:absolute;top:2px;left:2px;width:20px;height:20px;background:#fff;border-radius:50%;transition:0.3s;transform:${interview.job_board_enabled?'translateX(20px)':'translateX(0)'}"></div>
          </label>
        </div>

        <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:20px">
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Location</label>
            <input type="text" id="jb-location" value="${interview.location || ''}" placeholder="e.g. Remote, New York, NY"
              style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Job Type</label>
            <select id="jb-type" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              ${['full_time','part_time','contract','temporary','internship'].map(t =>
                `<option value="${t}" ${(interview.job_type||'full_time')===t?'selected':''}>${t.replace(/_/g,' ').replace(/\b\w/g,c=>c.toUpperCase())}</option>`
              ).join('')}
            </select>
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Salary Range</label>
            <input type="text" id="jb-salary" value="${interview.salary_range || ''}" placeholder="e.g. $40,000 - $60,000"
              style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Application Deadline</label>
            <input type="date" id="jb-deadline" value="${interview.application_deadline || ''}"
              style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
        </div>

        <button class="btn btn-primary" onclick="c31SaveJobBoard('${selectedId}')" style="width:100%">Save Job Board Settings</button>
      </div>

      <!-- Public URL Preview -->
      <div style="background:#f0fff0;border:1px solid #bbf7d0;border-radius:12px;padding:20px;text-align:center">
        <p style="font-size:13px;color:#065f46;margin-bottom:8px">Your public job board URL:</p>
        <code style="background:#fff;padding:8px 16px;border-radius:6px;font-size:14px;color:#111;user-select:all">${window.location.origin}/jobs/${interview.user_id || 'YOUR_AGENCY_ID'}</code>
        <button class="btn btn-sm btn-outline" style="margin-left:8px" onclick="navigator.clipboard.writeText('${window.location.origin}/jobs/${interview.user_id || ''}');toast('Copied!','success')">Copy</button>
      </div>
    </div>`;
}

async function c31SaveJobBoard(interviewId) {
  const enabled = document.getElementById('jb-enabled').checked;
  const location = document.getElementById('jb-location').value;
  const job_type = document.getElementById('jb-type').value;
  const salary_range = document.getElementById('jb-salary').value;
  const application_deadline = document.getElementById('jb-deadline').value;

  try {
    await api(`/api/interviews/${interviewId}/job-board`, {
      method: 'PUT',
      body: JSON.stringify({ enabled, location, job_type, salary_range, application_deadline })
    });
    toast('Job board settings saved', 'success');
  } catch(e) { toast(e.message, 'error'); }
}


// ==================== CYCLE 33: LEAD SOURCING ENGINE ====================

async function renderLeadSourcing() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading Find Candidates...</div>';

  const params = new URLSearchParams(window.location.search);
  const curState = params.get('state') || '';
  const curStatus = params.get('status') || '';
  const curSource = params.get('source') || '';
  const curSearch = params.get('search') || '';
  const curPage = parseInt(params.get('page') || '1');
  const curTab = params.get('tab') || 'leads';

  let leads = [], total = 0, totalPages = 1, analytics = {};
  try {
    const q = new URLSearchParams({page: curPage, per_page: 50});
    if (curState) q.set('state', curState);
    if (curStatus) q.set('status', curStatus);
    if (curSource) q.set('source', curSource);
    if (curSearch) q.set('search', curSearch);
    const data = await api('/api/leads?' + q.toString());
    leads = data.leads || [];
    total = data.total || 0;
    totalPages = data.total_pages || 1;
  } catch(e) {}
  try { analytics = await api('/api/leads/analytics'); } catch(e) {}

  const states = ['AL','FL','IN','TN','AZ','CA','GA','IL','MI','NC','NY','OH','PA','TX','VA'];
  const statusOpts = ['new','contacted','qualified','converted','rejected'];
  const sourceOpts = ['manual','csv_import','referral','indeed','google_jobs','other'];

  const leadStatusBadge = (s) => {
    const m = {new:'background:#e0f2fe;color:#0369a1',contacted:'background:#fef3c7;color:#92400e',
      qualified:'background:#d1fae5;color:#065f46',converted:'background:#0ace0a;color:#000',
      rejected:'background:#fee2e2;color:#991b1b'};
    return `<span style="padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;${m[s]||'background:#f3f4f6;color:#666'}">${(s||'new').replace(/_/g,' ')}</span>`;
  };

  // === Tab: Leads Table ===
  const leadsTab = `
    <!-- Filters -->
    <div style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px">
      <input type="text" id="ls-search" placeholder="Search name, email, phone..." value="${curSearch}"
        style="flex:1;min-width:200px;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
        onkeydown="if(event.key==='Enter')c33FilterLeads()">
      <select id="ls-state" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px" onchange="c33FilterLeads()">
        <option value="">All States</option>
        ${states.map(s => `<option value="${s}" ${s===curState?'selected':''}>${s}</option>`).join('')}
      </select>
      <select id="ls-status" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px" onchange="c33FilterLeads()">
        <option value="">All Statuses</option>
        ${statusOpts.map(s => `<option value="${s}" ${s===curStatus?'selected':''}>${s.replace(/_/g,' ')}</option>`).join('')}
      </select>
      <select id="ls-source" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px" onchange="c33FilterLeads()">
        <option value="">All Sources</option>
        ${sourceOpts.map(s => `<option value="${s}" ${s===curSource?'selected':''}>${s.replace(/_/g,' ')}</option>`).join('')}
      </select>
      <button class="btn btn-sm" onclick="c33FilterLeads()" style="background:#0ace0a;color:#000;font-weight:600;border:none;padding:8px 16px;border-radius:8px;cursor:pointer">Search</button>
    </div>

    <!-- Bulk Actions -->
    <div id="ls-bulk-bar" style="display:none;background:#f0fff0;border:1px solid #bbf7d0;border-radius:8px;padding:10px 16px;margin-bottom:12px;display:flex;align-items:center;gap:12px">
      <span id="ls-selected-count" style="font-size:13px;font-weight:600">0 selected</span>
      <button class="btn btn-sm" onclick="c33BulkConvert()" style="background:#0ace0a;color:#000;font-weight:600;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">Add to Candidates</button>
      <button class="btn btn-sm" onclick="c33BulkDelete()" style="background:#fee2e2;color:#991b1b;font-weight:600;border:none;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:12px">Delete Selected</button>
    </div>

    <!-- Leads Table -->
    ${leads.length ? `
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse">
        <thead>
          <tr style="background:#f9fafb">
            <th style="padding:10px 12px;text-align:left;width:40px"><input type="checkbox" onchange="c33SelectAll(this)"></th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">Name</th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">Contact</th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">Location</th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">License</th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">Source</th>
            <th style="padding:10px 12px;text-align:left;font-size:12px;font-weight:600;color:#666">Status</th>
            <th style="padding:10px 12px;text-align:right;font-size:12px;font-weight:600;color:#666">Actions</th>
          </tr>
        </thead>
        <tbody>
          ${leads.map(l => `
          <tr style="border-top:1px solid #f3f4f6" id="lead-row-${l.id}">
            <td style="padding:10px 12px"><input type="checkbox" class="ls-cb" value="${l.id}" onchange="c33UpdateBulkBar()"></td>
            <td style="padding:10px 12px">
              <div style="font-weight:600;font-size:13px">${l.first_name} ${l.last_name}</div>
              ${l.npn ? `<div style="font-size:11px;color:#666">NPN: ${l.npn}</div>` : ''}
            </td>
            <td style="padding:10px 12px;font-size:13px">
              ${l.email ? `<div>${l.email}</div>` : ''}
              ${l.phone ? `<div style="color:#666">${l.phone}</div>` : ''}
            </td>
            <td style="padding:10px 12px;font-size:13px">${[l.city, l.state].filter(Boolean).join(', ')}${l.zip_code ? ` ${l.zip_code}` : ''}</td>
            <td style="padding:10px 12px;font-size:13px">${l.license_type || '—'}</td>
            <td style="padding:10px 12px;font-size:12px;text-transform:capitalize">${(l.source||'').replace(/_/g,' ')}</td>
            <td style="padding:10px 12px">${leadStatusBadge(l.status)}</td>
            <td style="padding:10px 12px;text-align:right">
              <button onclick="c33ConvertLead('${l.id}')" title="Add to Candidates" style="background:none;border:none;cursor:pointer;padding:4px;color:#0ace0a;font-size:16px">&#x2795;</button>
              <button onclick="c33EditLead('${l.id}')" title="Edit" style="background:none;border:none;cursor:pointer;padding:4px;color:#666;font-size:16px">&#x270E;</button>
              <button onclick="c33DeleteLead('${l.id}')" title="Delete" style="background:none;border:none;cursor:pointer;padding:4px;color:#ef4444;font-size:16px">&#x2716;</button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>

    <!-- Pagination -->
    ${totalPages > 1 ? `
    <div style="display:flex;justify-content:center;gap:8px;margin-top:16px">
      ${curPage > 1 ? `<button class="btn btn-sm btn-outline" onclick="c33GoPage(${curPage-1})">Prev</button>` : ''}
      <span style="padding:6px 12px;font-size:13px;color:#666">Page ${curPage} of ${totalPages} (${total} leads)</span>
      ${curPage < totalPages ? `<button class="btn btn-sm btn-outline" onclick="c33GoPage(${curPage+1})">Next</button>` : ''}
    </div>` : `<div style="text-align:center;margin-top:8px;font-size:13px;color:#666">${total} lead${total!==1?'s':''} total</div>`}
    ` : `
    <div style="text-align:center;padding:60px 20px;background:#fff;border:1px solid #e5e7eb;border-radius:12px">
      <div style="font-size:48px;margin-bottom:16px">&#x1F50D;</div>
      <h3 style="font-size:18px;font-weight:600;margin-bottom:8px">No leads yet</h3>
      <p style="color:#666;font-size:14px;margin-bottom:20px">Import a CSV, add leads manually, or search by ZIP code to get started.</p>
    </div>`}
  `;

  // === Tab: Import ===
  const importTab = `
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
      <h3 style="font-size:16px;font-weight:600;margin-bottom:4px">Import Leads from CSV</h3>
      <p style="font-size:13px;color:#666;margin-bottom:20px">Upload a CSV file with lead data. Supports licensed agent lists and custom formats.</p>
      <div id="ls-drop-zone" style="border:2px dashed #d1d5db;border-radius:12px;padding:40px;text-align:center;cursor:pointer;transition:all 0.2s"
        onclick="document.getElementById('ls-csv-input').click()"
        ondragover="event.preventDefault();this.style.borderColor='#0ace0a';this.style.background='#f0fff0'"
        ondragleave="this.style.borderColor='#d1d5db';this.style.background='transparent'"
        ondrop="event.preventDefault();this.style.borderColor='#d1d5db';this.style.background='transparent';c33HandleCSV(event.dataTransfer.files[0])">
        <div style="font-size:36px;margin-bottom:8px">&#x1F4C1;</div>
        <p style="font-weight:600;margin-bottom:4px">Drop CSV file here or click to browse</p>
        <p style="font-size:12px;color:#999">Supports: first_name, last_name, email, phone, zip_code, state, license_type, license_number, npn</p>
        <input type="file" id="ls-csv-input" accept=".csv" style="display:none" onchange="c33HandleCSV(this.files[0])">
      </div>

      <!-- Column Mapping (hidden until file loaded) -->
      <div id="ls-mapping-area" style="display:none;margin-top:20px">
        <h4 style="font-size:14px;font-weight:600;margin-bottom:12px">Column Mapping</h4>
        <div id="ls-mapping-fields" style="display:grid;grid-template-columns:1fr 1fr;gap:12px"></div>
        <div id="ls-preview-area" style="margin-top:16px"></div>
        <button class="btn btn-primary" onclick="c33SubmitImport()" style="margin-top:16px;width:100%;background:#0ace0a;color:#000;font-weight:600;border:none;padding:12px;border-radius:8px;cursor:pointer;font-size:14px">Import Leads</button>
      </div>
    </div>

    <!-- Import History -->
    <div id="ls-import-history" style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px">
      <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Import History</h3>
      <div id="ls-history-list"><div class="loading-spinner"><div class="spinner"></div></div></div>
    </div>
  `;

  // === Tab: Add Lead ===
  const addTab = `
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;max-width:600px">
      <h3 style="font-size:16px;font-weight:600;margin-bottom:20px">Add Lead Manually</h3>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">First Name *</label>
          <input type="text" id="ls-fn" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Last Name *</label>
          <input type="text" id="ls-ln" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Email</label>
          <input type="email" id="ls-email" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Phone</label>
          <input type="text" id="ls-phone" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">City</label>
          <input type="text" id="ls-city" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">State</label>
          <select id="ls-st" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
            <option value="">Select...</option>
            ${states.map(s => `<option value="${s}">${s}</option>`).join('')}
          </select>
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">ZIP Code</label>
          <input type="text" id="ls-zip" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">License Type</label>
          <input type="text" id="ls-lic" placeholder="e.g. Life & Health" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">License Number</label>
          <input type="text" id="ls-licnum" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">NPN</label>
          <input type="text" id="ls-npn" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        </div>
      </div>
      <div style="margin-top:16px">
        <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Notes</label>
        <textarea id="ls-notes" rows="3" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;resize:vertical"></textarea>
      </div>
      <button onclick="c33AddLead()" style="margin-top:16px;width:100%;background:#0ace0a;color:#000;font-weight:700;border:none;padding:12px;border-radius:8px;cursor:pointer;font-size:14px">Add Lead</button>
    </div>
  `;

  // === Tab: ZIP Search ===
  const zipTab = `
    <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
      <h3 style="font-size:16px;font-weight:600;margin-bottom:4px">ZIP Code Search</h3>
      <p style="font-size:13px;color:#666;margin-bottom:20px">Search your imported leads by ZIP code with radius matching.</p>
      <div style="display:flex;gap:12px;align-items:end">
        <div style="flex:1">
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">ZIP Code</label>
          <input type="text" id="ls-zip-search" placeholder="e.g. 37201" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"
            onkeydown="if(event.key==='Enter')c33ZipSearch()">
        </div>
        <div>
          <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Radius</label>
          <select id="ls-zip-radius" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
            <option value="exact">Exact Match</option>
            <option value="nearby">Nearby (same 3-digit prefix)</option>
            <option value="region">Region (same 2-digit prefix)</option>
          </select>
        </div>
        <button onclick="c33ZipSearch()" style="background:#0ace0a;color:#000;font-weight:600;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;height:38px">Search</button>
      </div>
    </div>
    <div id="ls-zip-results"></div>
  `;

  content.innerHTML = `
    <div style="max-width:1100px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Find Candidates</h2>
          <p style="color:#666;font-size:13px">${total} lead${total!==1?'s':''} in database</p>
        </div>
      </div>

      <!-- Analytics Cards -->
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px">
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#111">${analytics.total||0}</div>
          <div style="font-size:12px;color:#666;margin-top:4px">Total Leads</div>
        </div>
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#0369a1">${(analytics.by_status||{}).new||0}</div>
          <div style="font-size:12px;color:#666;margin-top:4px">New</div>
        </div>
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#92400e">${(analytics.by_status||{}).contacted||0}</div>
          <div style="font-size:12px;color:#666;margin-top:4px">Contacted</div>
        </div>
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#065f46">${(analytics.by_status||{}).qualified||0}</div>
          <div style="font-size:12px;color:#666;margin-top:4px">Qualified</div>
        </div>
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px;text-align:center">
          <div style="font-size:24px;font-weight:700;color:#0ace0a">${analytics.conversion_rate||0}%</div>
          <div style="font-size:12px;color:#666;margin-top:4px">Conversion Rate</div>
        </div>
      </div>

      <!-- Tabs -->
      <div style="display:flex;gap:0;border-bottom:2px solid #e5e7eb;margin-bottom:20px">
        <button class="ls-tab" data-tab="leads" onclick="c33SwitchTab('leads')" style="padding:10px 20px;font-size:13px;font-weight:600;border:none;background:none;cursor:pointer;border-bottom:2px solid ${curTab==='leads'?'#0ace0a':'transparent'};color:${curTab==='leads'?'#0ace0a':'#666'};margin-bottom:-2px">All Leads</button>
        <button class="ls-tab" data-tab="import" onclick="c33SwitchTab('import')" style="padding:10px 20px;font-size:13px;font-weight:600;border:none;background:none;cursor:pointer;border-bottom:2px solid ${curTab==='import'?'#0ace0a':'transparent'};color:${curTab==='import'?'#0ace0a':'#666'};margin-bottom:-2px">Import CSV</button>
        <button class="ls-tab" data-tab="add" onclick="c33SwitchTab('add')" style="padding:10px 20px;font-size:13px;font-weight:600;border:none;background:none;cursor:pointer;border-bottom:2px solid ${curTab==='add'?'#0ace0a':'transparent'};color:${curTab==='add'?'#0ace0a':'#666'};margin-bottom:-2px">Add Lead</button>
        <button class="ls-tab" data-tab="zip" onclick="c33SwitchTab('zip')" style="padding:10px 20px;font-size:13px;font-weight:600;border:none;background:none;cursor:pointer;border-bottom:2px solid ${curTab==='zip'?'#0ace0a':'transparent'};color:${curTab==='zip'?'#0ace0a':'#666'};margin-bottom:-2px">ZIP Search</button>
      </div>

      <!-- Tab Content -->
      <div id="ls-tab-leads" style="display:${curTab==='leads'?'block':'none'}">${leadsTab}</div>
      <div id="ls-tab-import" style="display:${curTab==='import'?'block':'none'}">${importTab}</div>
      <div id="ls-tab-add" style="display:${curTab==='add'?'block':'none'}">${addTab}</div>
      <div id="ls-tab-zip" style="display:${curTab==='zip'?'block':'none'}">${zipTab}</div>
    </div>`;

  // Load import history if on import tab
  if (curTab === 'import') c33LoadImportHistory();
}

// --- C33 Lead Sourcing Helper Functions ---

let c33CsvData = null;
let c33CsvFilename = '';

function c33SwitchTab(tab) {
  document.querySelectorAll('[id^="ls-tab-"]').forEach(el => el.style.display = 'none');
  document.querySelectorAll('.ls-tab').forEach(el => {
    el.style.borderBottomColor = 'transparent';
    el.style.color = '#666';
  });
  const tabEl = document.getElementById('ls-tab-' + tab);
  if (tabEl) tabEl.style.display = 'block';
  const btn = document.querySelector(`.ls-tab[data-tab="${tab}"]`);
  if (btn) { btn.style.borderBottomColor = '#0ace0a'; btn.style.color = '#0ace0a'; }
  const p = new URLSearchParams(window.location.search);
  p.set('tab', tab);
  history.replaceState(null, '', '/lead-sourcing?' + p.toString());
  if (tab === 'import') c33LoadImportHistory();
}

function c33FilterLeads() {
  const p = new URLSearchParams();
  const search = document.getElementById('ls-search')?.value || '';
  const state = document.getElementById('ls-state')?.value || '';
  const status = document.getElementById('ls-status')?.value || '';
  const source = document.getElementById('ls-source')?.value || '';
  if (search) p.set('search', search);
  if (state) p.set('state', state);
  if (status) p.set('status', status);
  if (source) p.set('source', source);
  p.set('tab', 'leads');
  history.replaceState(null, '', '/lead-sourcing?' + p.toString());
  renderLeadSourcing();
}

function c33GoPage(page) {
  const p = new URLSearchParams(window.location.search);
  p.set('page', page);
  history.replaceState(null, '', '/lead-sourcing?' + p.toString());
  renderLeadSourcing();
}

function c33SelectAll(cb) {
  document.querySelectorAll('.ls-cb').forEach(el => el.checked = cb.checked);
  c33UpdateBulkBar();
}

function c33UpdateBulkBar() {
  const checked = document.querySelectorAll('.ls-cb:checked');
  const bar = document.getElementById('ls-bulk-bar');
  const cnt = document.getElementById('ls-selected-count');
  if (bar) bar.style.display = checked.length ? 'flex' : 'none';
  if (cnt) cnt.textContent = checked.length + ' selected';
}

async function c33AddLead() {
  const fn = document.getElementById('ls-fn')?.value?.trim();
  const ln = document.getElementById('ls-ln')?.value?.trim();
  if (!fn || !ln) { toast('First and last name are required', 'error'); return; }
  try {
    await api('/api/leads', {
      method: 'POST',
      body: JSON.stringify({
        first_name: fn, last_name: ln,
        email: document.getElementById('ls-email')?.value?.trim() || '',
        phone: document.getElementById('ls-phone')?.value?.trim() || '',
        city: document.getElementById('ls-city')?.value?.trim() || '',
        state: document.getElementById('ls-st')?.value || '',
        zip_code: document.getElementById('ls-zip')?.value?.trim() || '',
        license_type: document.getElementById('ls-lic')?.value?.trim() || '',
        license_number: document.getElementById('ls-licnum')?.value?.trim() || '',
        npn: document.getElementById('ls-npn')?.value?.trim() || '',
        notes: document.getElementById('ls-notes')?.value?.trim() || '',
        source: 'manual'
      })
    });
    toast('Lead added successfully', 'success');
    const p = new URLSearchParams(window.location.search);
    p.set('tab', 'leads');
    history.replaceState(null, '', '/lead-sourcing?' + p.toString());
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33DeleteLead(id) {
  if (!confirm('Delete this lead?')) return;
  try {
    await api('/api/leads/' + id, { method: 'DELETE' });
    const row = document.getElementById('lead-row-' + id);
    if (row) row.remove();
    toast('Lead deleted', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function c33ConvertLead(id) {
  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  if (!interviews.length) { toast('Create an interview first', 'error'); return; }
  const iid = interviews[0].id;
  const pick = prompt('Enter interview ID to convert to (default: ' + interviews[0].title + '):', iid);
  if (!pick) return;
  try {
    const res = await api('/api/leads/' + id + '/convert', { method: 'POST', body: JSON.stringify({ interview_id: pick }) });
    toast('Lead converted to candidate!', 'success');
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33EditLead(id) {
  try {
    const lead = await api('/api/leads/' + id);
    const modal = document.createElement('div');
    modal.id = 'ls-edit-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
    modal.innerHTML = `
      <div style="background:#fff;border-radius:16px;padding:28px;width:500px;max-width:90vw;max-height:80vh;overflow:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h3 style="font-size:18px;font-weight:700">Edit Lead</h3>
          <button onclick="document.getElementById('ls-edit-modal').remove()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#666">&times;</button>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">First Name</label><input id="le-fn" value="${lead.first_name||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Last Name</label><input id="le-ln" value="${lead.last_name||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Email</label><input id="le-email" value="${lead.email||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Phone</label><input id="le-phone" value="${lead.phone||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">City</label><input id="le-city" value="${lead.city||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">State</label><input id="le-state" value="${lead.state||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">ZIP</label><input id="le-zip" value="${lead.zip_code||''}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px"></div>
          <div><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Status</label>
            <select id="le-status" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              ${['new','contacted','qualified','converted','rejected'].map(s => `<option value="${s}" ${s===lead.status?'selected':''}>${s}</option>`).join('')}
            </select>
          </div>
        </div>
        <div style="margin-top:12px"><label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Notes</label><textarea id="le-notes" rows="3" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px;resize:vertical">${lead.notes||''}</textarea></div>
        <button onclick="c33SaveEdit('${id}')" style="margin-top:16px;width:100%;background:#0ace0a;color:#000;font-weight:700;border:none;padding:12px;border-radius:8px;cursor:pointer;font-size:14px">Save Changes</button>
      </div>`;
    document.body.appendChild(modal);
  } catch(e) { toast(e.message, 'error'); }
}

async function c33SaveEdit(id) {
  try {
    await api('/api/leads/' + id, {
      method: 'PUT',
      body: JSON.stringify({
        first_name: document.getElementById('le-fn')?.value,
        last_name: document.getElementById('le-ln')?.value,
        email: document.getElementById('le-email')?.value,
        phone: document.getElementById('le-phone')?.value,
        city: document.getElementById('le-city')?.value,
        state: document.getElementById('le-state')?.value,
        zip_code: document.getElementById('le-zip')?.value,
        status: document.getElementById('le-status')?.value,
        notes: document.getElementById('le-notes')?.value
      })
    });
    document.getElementById('ls-edit-modal')?.remove();
    toast('Lead updated', 'success');
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33BulkDelete() {
  const ids = [...document.querySelectorAll('.ls-cb:checked')].map(cb => cb.value);
  if (!ids.length) return;
  if (!confirm(`Delete ${ids.length} lead(s)?`)) return;
  try {
    await api('/api/leads/bulk-delete', { method: 'POST', body: JSON.stringify({ lead_ids: ids }) });
    toast(ids.length + ' leads deleted', 'success');
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33BulkConvert() {
  const ids = [...document.querySelectorAll('.ls-cb:checked')].map(cb => cb.value);
  if (!ids.length) return;
  let interviews = [];
  try { interviews = await api('/api/interviews'); } catch(e) {}
  if (!interviews.length) { toast('Create an interview first', 'error'); return; }
  const iid = prompt('Enter interview ID to convert to (default: ' + interviews[0].title + '):', interviews[0].id);
  if (!iid) return;
  try {
    const res = await api('/api/leads/bulk-convert', { method: 'POST', body: JSON.stringify({ lead_ids: ids, interview_id: iid }) });
    toast(`${res.converted||0} leads converted to pipeline`, 'success');
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

function c33HandleCSV(file) {
  if (!file) return;
  c33CsvFilename = file.name;
  const reader = new FileReader();
  reader.onload = function(e) {
    const text = e.target.result;
    const lines = text.split(/\r?\n/).filter(l => l.trim());
    if (lines.length < 2) { toast('CSV must have a header row + data', 'error'); return; }
    const headers = lines[0].split(',').map(h => h.trim().replace(/^"|"$/g, ''));
    const rows = [];
    for (let i = 1; i < lines.length; i++) {
      const vals = lines[i].split(',').map(v => v.trim().replace(/^"|"$/g, ''));
      const row = {};
      headers.forEach((h, idx) => row[h] = vals[idx] || '');
      rows.push(row);
    }
    c33CsvData = { headers, rows };
    c33ShowMapping(headers, rows);
  };
  reader.readAsText(file);
}

function c33ShowMapping(headers, rows) {
  const fields = ['first_name','last_name','email','phone','zip_code','city','state','license_type','license_number','npn'];
  const area = document.getElementById('ls-mapping-fields');
  const mapArea = document.getElementById('ls-mapping-area');
  if (mapArea) mapArea.style.display = 'block';
  if (!area) return;

  area.innerHTML = fields.map(f => {
    const best = headers.find(h => h.toLowerCase().replace(/[\s_-]/g,'') === f.replace(/_/g,'')) || '';
    return `<div>
      <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">${f.replace(/_/g,' ')}</label>
      <select id="ls-map-${f}" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
        <option value="">-- Skip --</option>
        ${headers.map(h => `<option value="${h}" ${h===best?'selected':''}>${h}</option>`).join('')}
      </select>
    </div>`;
  }).join('');

  const preview = document.getElementById('ls-preview-area');
  if (preview) {
    const sample = rows.slice(0, 3);
    preview.innerHTML = `<p style="font-size:13px;font-weight:600;margin-bottom:8px">${rows.length} rows found. Preview:</p>
      <div style="overflow-x:auto;border:1px solid #e5e7eb;border-radius:8px">
        <table style="width:100%;border-collapse:collapse;font-size:12px">
          <thead><tr style="background:#f9fafb">${headers.map(h => `<th style="padding:6px 10px;text-align:left;white-space:nowrap">${h}</th>`).join('')}</tr></thead>
          <tbody>${sample.map(r => `<tr>${headers.map(h => `<td style="padding:6px 10px;border-top:1px solid #f3f4f6;white-space:nowrap">${r[h]||''}</td>`).join('')}</tr>`).join('')}</tbody>
        </table>
      </div>`;
  }
}

async function c33SubmitImport() {
  if (!c33CsvData || !c33CsvData.rows.length) { toast('No CSV data loaded', 'error'); return; }
  const fields = ['first_name','last_name','email','phone','zip_code','city','state','license_type','license_number','npn'];
  const mapping = {};
  fields.forEach(f => {
    const val = document.getElementById('ls-map-' + f)?.value;
    if (val) mapping[f] = val;
  });
  try {
    const res = await api('/api/leads/import', {
      method: 'POST',
      body: JSON.stringify({ rows: c33CsvData.rows, column_mapping: mapping, filename: c33CsvFilename })
    });
    toast(`Imported ${res.imported} leads (${res.duplicates} duplicates, ${res.errors} errors)`, 'success');
    c33CsvData = null;
    c33CsvFilename = '';
    const p = new URLSearchParams(window.location.search);
    p.set('tab', 'leads');
    history.replaceState(null, '', '/lead-sourcing?' + p.toString());
    renderLeadSourcing();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33LoadImportHistory() {
  const el = document.getElementById('ls-history-list');
  if (!el) return;
  try {
    const data = await api('/api/leads/import-history');
    const batches = data.batches || [];
    if (!batches.length) {
      el.innerHTML = '<p style="color:#999;text-align:center;padding:20px">No imports yet</p>';
      return;
    }
    el.innerHTML = `<table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9fafb">
        <th style="padding:8px 12px;text-align:left">File</th>
        <th style="padding:8px 12px;text-align:right">Total</th>
        <th style="padding:8px 12px;text-align:right">Imported</th>
        <th style="padding:8px 12px;text-align:right">Duplicates</th>
        <th style="padding:8px 12px;text-align:right">Errors</th>
        <th style="padding:8px 12px;text-align:right">Date</th>
      </tr></thead>
      <tbody>${batches.map(b => `<tr style="border-top:1px solid #f3f4f6">
        <td style="padding:8px 12px;font-weight:500">${b.filename||'—'}</td>
        <td style="padding:8px 12px;text-align:right">${b.total_rows||0}</td>
        <td style="padding:8px 12px;text-align:right;color:#065f46;font-weight:600">${b.imported_rows||0}</td>
        <td style="padding:8px 12px;text-align:right;color:#92400e">${b.duplicate_rows||0}</td>
        <td style="padding:8px 12px;text-align:right;color:#991b1b">${b.error_rows||0}</td>
        <td style="padding:8px 12px;text-align:right;color:#666">${formatDate(b.created_at)}</td>
      </tr>`).join('')}</tbody>
    </table>`;
  } catch(e) { el.innerHTML = '<p style="color:#ef4444">Failed to load import history</p>'; }
}

async function c33ZipSearch() {
  const zip = document.getElementById('ls-zip-search')?.value?.trim();
  const radius = document.getElementById('ls-zip-radius')?.value || 'exact';
  const resultsEl = document.getElementById('ls-zip-results');
  if (!zip || zip.length < 3) { toast('Enter at least 3 digits', 'error'); return; }
  if (resultsEl) resultsEl.innerHTML = '<div class="loading-spinner"><div class="spinner"></div></div>';
  try {
    const data = await api(`/api/leads/zip-search?zip_code=${zip}&radius=${radius}`);
    const leads = data.leads || [];
    if (!leads.length) {
      resultsEl.innerHTML = '<div style="text-align:center;padding:40px;background:#fff;border:1px solid #e5e7eb;border-radius:12px"><p style="color:#666">No leads found for ZIP ' + zip + ' (' + radius + ')</p></div>';
      return;
    }
    resultsEl.innerHTML = `
      <div style="margin-bottom:12px;font-size:13px;color:#666">${leads.length} lead${leads.length!==1?'s':''} found for ${zip} (${radius})</div>
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="background:#f9fafb">
            <th style="padding:10px 12px;text-align:left">Name</th>
            <th style="padding:10px 12px;text-align:left">Email</th>
            <th style="padding:10px 12px;text-align:left">Phone</th>
            <th style="padding:10px 12px;text-align:left">ZIP</th>
            <th style="padding:10px 12px;text-align:left">License</th>
            <th style="padding:10px 12px;text-align:left">Status</th>
          </tr></thead>
          <tbody>${leads.map(l => `<tr style="border-top:1px solid #f3f4f6">
            <td style="padding:8px 12px;font-weight:600">${l.first_name} ${l.last_name}</td>
            <td style="padding:8px 12px">${l.email||'—'}</td>
            <td style="padding:8px 12px">${l.phone||'—'}</td>
            <td style="padding:8px 12px">${l.zip_code||'—'}</td>
            <td style="padding:8px 12px">${l.license_type||'—'}</td>
            <td style="padding:8px 12px;text-transform:capitalize">${l.status||'new'}</td>
          </tr>`).join('')}</tbody>
        </table>
      </div>`;
  } catch(e) { if (resultsEl) resultsEl.innerHTML = '<p style="color:#ef4444">' + e.message + '</p>'; }
}


// --- C33 Feature 2: Referral Links ---

async function renderReferralLinks() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading Referral Links...</div>';

  let links = [], interviews = [];
  try { const d = await api('/api/referral-links'); links = d.links || []; } catch(e) {}
  try { interviews = await api('/api/interviews'); } catch(e) {}

  content.innerHTML = `
    <div style="max-width:900px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Referral Links</h2>
          <p style="color:#666;font-size:13px">Create trackable referral links for your interviews</p>
        </div>
      </div>

      <!-- Create New -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Create Referral Link</h3>
        <div style="display:flex;gap:12px;align-items:end;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Interview</label>
            <select id="rl-interview" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="">Select interview...</option>
              ${interviews.map(i => `<option value="${i.id}">${i.title}</option>`).join('')}
            </select>
          </div>
          <div style="flex:1;min-width:200px">
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Label (optional)</label>
            <input type="text" id="rl-label" placeholder="e.g. LinkedIn Campaign" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
          </div>
          <button onclick="c33CreateRefLink()" style="background:#0ace0a;color:#000;font-weight:600;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;height:38px">Create Link</button>
        </div>
      </div>

      <!-- Existing Links -->
      ${links.length ? `
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="background:#f9fafb">
            <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#666">Label</th>
            <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#666">Referral URL</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Clicks</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Applications</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Hires</th>
            <th style="padding:10px 16px;text-align:center;font-size:12px;font-weight:600;color:#666">Active</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Actions</th>
          </tr></thead>
          <tbody>
            ${links.map(l => {
              const url = window.location.origin + '/apply/' + l.interview_id + '?ref=' + l.code;
              return `<tr style="border-top:1px solid #f3f4f6">
                <td style="padding:10px 16px;font-weight:600;font-size:13px">${l.label||l.code}</td>
                <td style="padding:10px 16px;font-size:12px">
                  <code style="background:#f3f4f6;padding:2px 8px;border-radius:4px;user-select:all;font-size:11px">${url}</code>
                  <button onclick="navigator.clipboard.writeText('${url}');toast('Copied!','success')" style="background:none;border:none;cursor:pointer;color:#0ace0a;font-size:14px;margin-left:4px" title="Copy">&#x1F4CB;</button>
                </td>
                <td style="padding:10px 16px;text-align:right;font-weight:600">${l.clicks||0}</td>
                <td style="padding:10px 16px;text-align:right;font-weight:600">${l.applications||0}</td>
                <td style="padding:10px 16px;text-align:right;font-weight:600;color:#0ace0a">${l.hires||0}</td>
                <td style="padding:10px 16px;text-align:center">${l.is_active ? '<span style="color:#0ace0a;font-weight:700">Yes</span>' : '<span style="color:#999">No</span>'}</td>
                <td style="padding:10px 16px;text-align:right">
                  <button onclick="c33ViewRefStats('${l.id}')" style="background:none;border:none;cursor:pointer;color:#0369a1;font-size:13px;text-decoration:underline">Stats</button>
                  <button onclick="c33DeleteRefLink('${l.id}')" style="background:none;border:none;cursor:pointer;color:#ef4444;font-size:16px;margin-left:8px">&times;</button>
                </td>
              </tr>`;
            }).join('')}
          </tbody>
        </table>
      </div>` : `
      <div style="text-align:center;padding:60px 20px;background:#fff;border:1px solid #e5e7eb;border-radius:12px">
        <div style="font-size:48px;margin-bottom:16px">&#x1F517;</div>
        <h3 style="font-size:18px;font-weight:600;margin-bottom:8px">No referral links yet</h3>
        <p style="color:#666;font-size:14px">Create your first referral link above to start tracking candidate sources.</p>
      </div>`}
    </div>`;
}

async function c33CreateRefLink() {
  const iid = document.getElementById('rl-interview')?.value;
  const label = document.getElementById('rl-label')?.value?.trim();
  if (!iid) { toast('Select an interview', 'error'); return; }
  try {
    const res = await api('/api/referral-links', {
      method: 'POST',
      body: JSON.stringify({ interview_id: iid, label: label || '' })
    });
    toast('Referral link created!', 'success');
    renderReferralLinks();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33DeleteRefLink(id) {
  if (!confirm('Delete this referral link?')) return;
  try {
    await api('/api/referral-links/' + id, { method: 'DELETE' });
    toast('Link deleted', 'success');
    renderReferralLinks();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33ViewRefStats(id) {
  try {
    const data = await api('/api/referral-links/' + id + '/stats');
    const link = data.link;
    const candidates = data.candidates || [];
    const modal = document.createElement('div');
    modal.id = 'rl-stats-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);z-index:9999;display:flex;align-items:center;justify-content:center';
    modal.innerHTML = `
      <div style="background:#fff;border-radius:16px;padding:28px;width:550px;max-width:90vw;max-height:80vh;overflow:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h3 style="font-size:18px;font-weight:700">Referral Stats: ${link.label||link.code}</h3>
          <button onclick="document.getElementById('rl-stats-modal').remove()" style="background:none;border:none;font-size:20px;cursor:pointer;color:#666">&times;</button>
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px;margin-bottom:20px">
          <div style="background:#f0fff0;border-radius:10px;padding:16px;text-align:center">
            <div style="font-size:24px;font-weight:700">${link.clicks||0}</div>
            <div style="font-size:12px;color:#666">Clicks</div>
          </div>
          <div style="background:#e0f2fe;border-radius:10px;padding:16px;text-align:center">
            <div style="font-size:24px;font-weight:700">${link.applications||0}</div>
            <div style="font-size:12px;color:#666">Applications</div>
          </div>
          <div style="background:#d1fae5;border-radius:10px;padding:16px;text-align:center">
            <div style="font-size:24px;font-weight:700;color:#0ace0a">${link.hires||0}</div>
            <div style="font-size:12px;color:#666">Hires</div>
          </div>
        </div>
        ${candidates.length ? `
        <h4 style="font-size:14px;font-weight:600;margin-bottom:8px">Candidates from this link</h4>
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead><tr style="background:#f9fafb"><th style="padding:8px 12px;text-align:left">Name</th><th style="padding:8px 12px;text-align:left">Email</th><th style="padding:8px 12px;text-align:left">Stage</th><th style="padding:8px 12px;text-align:right">Date</th></tr></thead>
          <tbody>${candidates.map(c => `<tr style="border-top:1px solid #f3f4f6"><td style="padding:6px 12px;font-weight:500">${c.name||'—'}</td><td style="padding:6px 12px">${c.email||'—'}</td><td style="padding:6px 12px;text-transform:capitalize">${(c.pipeline_stage||'').replace(/_/g,' ')}</td><td style="padding:6px 12px;text-align:right;color:#666">${formatDate(c.created_at)}</td></tr>`).join('')}</tbody>
        </table>` : '<p style="color:#999;text-align:center;padding:16px">No candidates from this link yet</p>'}
      </div>`;
    document.body.appendChild(modal);
  } catch(e) { toast(e.message, 'error'); }
}


// --- C33 Feature 3: Job Syndication ---

async function renderJobSyndication() {
  content.innerHTML = '<div class="loading-spinner"><div class="spinner"></div>Loading Post to Job Sites...</div>';

  let syndications = [], interviews = [];
  try { const d = await api('/api/job-syndication'); syndications = d.syndications || []; } catch(e) {}
  try { interviews = await api('/api/interviews'); } catch(e) {}

  content.innerHTML = `
    <div style="max-width:900px">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:24px">
        <div>
          <h2 style="font-size:22px;font-weight:700;margin-bottom:4px">Post to Job Sites</h2>
          <p style="color:#666;font-size:13px">Push your open positions to Indeed, Google Jobs, and more</p>
        </div>
      </div>

      <!-- Syndicate New -->
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:24px;margin-bottom:24px">
        <h3 style="font-size:16px;font-weight:600;margin-bottom:16px">Syndicate a Position</h3>
        <div style="display:flex;gap:12px;align-items:end;flex-wrap:wrap">
          <div style="flex:1;min-width:200px">
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Interview / Position</label>
            <select id="js-interview" style="width:100%;padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="">Select position...</option>
              ${interviews.map(i => `<option value="${i.id}">${i.title} ${i.position ? '(' + i.position + ')' : ''}</option>`).join('')}
            </select>
          </div>
          <div>
            <label style="font-size:12px;font-weight:600;color:#666;display:block;margin-bottom:4px">Platform</label>
            <select id="js-platform" style="padding:8px 12px;border:1px solid #e5e7eb;border-radius:8px;font-size:13px">
              <option value="indeed">Indeed</option>
              <option value="google_jobs">Google for Jobs</option>
            </select>
          </div>
          <button onclick="c33CreateSyndication()" style="background:#0ace0a;color:#000;font-weight:600;border:none;padding:8px 20px;border-radius:8px;cursor:pointer;font-size:13px;height:38px">Post Job</button>
        </div>
      </div>

      <!-- Active Postings -->
      ${syndications.length ? `
      <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;overflow:hidden;margin-bottom:24px">
        <table style="width:100%;border-collapse:collapse">
          <thead><tr style="background:#f9fafb">
            <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#666">Position</th>
            <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#666">Platform</th>
            <th style="padding:10px 16px;text-align:left;font-size:12px;font-weight:600;color:#666">Status</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Clicks</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Applications</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Posted</th>
            <th style="padding:10px 16px;text-align:right;font-size:12px;font-weight:600;color:#666">Actions</th>
          </tr></thead>
          <tbody>
            ${syndications.map(s => `<tr style="border-top:1px solid #f3f4f6">
              <td style="padding:10px 16px;font-weight:600;font-size:13px">${s.interview_title||'—'}</td>
              <td style="padding:10px 16px;font-size:13px">
                <span style="padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;${s.platform==='indeed'?'background:#e0f2fe;color:#0369a1':'background:#fef3c7;color:#92400e'}">${s.platform==='google_jobs'?'Google Jobs':s.platform.charAt(0).toUpperCase()+s.platform.slice(1)}</span>
              </td>
              <td style="padding:10px 16px"><span style="padding:2px 10px;border-radius:9999px;font-size:11px;font-weight:600;${s.status==='active'?'background:#d1fae5;color:#065f46':'background:#fee2e2;color:#991b1b'}">${s.status}</span></td>
              <td style="padding:10px 16px;text-align:right;font-weight:600">${s.clicks||0}</td>
              <td style="padding:10px 16px;text-align:right;font-weight:600">${s.applications||0}</td>
              <td style="padding:10px 16px;text-align:right;color:#666;font-size:12px">${formatDate(s.posted_at)}</td>
              <td style="padding:10px 16px;text-align:right">
                <button onclick="c33DeleteSyndication('${s.id}')" style="background:none;border:none;cursor:pointer;color:#ef4444;font-size:16px" title="Remove">&times;</button>
              </td>
            </tr>`).join('')}
          </tbody>
        </table>
      </div>` : `
      <div style="text-align:center;padding:60px 20px;background:#fff;border:1px solid #e5e7eb;border-radius:12px;margin-bottom:24px">
        <div style="font-size:48px;margin-bottom:16px">&#x1F4E1;</div>
        <h3 style="font-size:18px;font-weight:600;margin-bottom:8px">No syndications yet</h3>
        <p style="color:#666;font-size:14px">Select an interview and platform above to start syndicating your job postings.</p>
      </div>`}

      <!-- Feed URLs -->
      <div style="background:#f0fff0;border:1px solid #bbf7d0;border-radius:12px;padding:20px">
        <h3 style="font-size:15px;font-weight:600;margin-bottom:12px;color:#065f46">Feed URLs</h3>
        <div style="margin-bottom:12px">
          <span style="font-size:12px;font-weight:600;color:#666">Indeed XML Feed:</span>
          <code style="background:#fff;padding:4px 10px;border-radius:6px;font-size:12px;color:#111;display:inline-block;margin-top:4px;user-select:all">${window.location.origin}/indeed-feed.xml</code>
          <button onclick="navigator.clipboard.writeText('${window.location.origin}/indeed-feed.xml');toast('Copied!','success')" style="background:none;border:none;cursor:pointer;color:#0ace0a;font-size:13px;margin-left:4px">Copy</button>
        </div>
        <p style="font-size:12px;color:#666">Google for Jobs structured data is automatically embedded on your interview pages when enabled.</p>
      </div>
    </div>`;
}

async function c33CreateSyndication() {
  const iid = document.getElementById('js-interview')?.value;
  const platform = document.getElementById('js-platform')?.value;
  if (!iid) { toast('Select a position', 'error'); return; }
  try {
    await api('/api/job-syndication', {
      method: 'POST',
      body: JSON.stringify({ interview_id: iid, platform: platform })
    });
    toast('Position syndicated to ' + (platform === 'google_jobs' ? 'Google Jobs' : 'Indeed') + '!', 'success');
    renderJobSyndication();
  } catch(e) { toast(e.message, 'error'); }
}

async function c33DeleteSyndication(id) {
  if (!confirm('Remove this syndication?')) return;
  try {
    await api('/api/job-syndication/' + id, { method: 'DELETE' });
    toast('Posting removed', 'success');
    renderJobSyndication();
  } catch(e) { toast(e.message, 'error'); }
}


// ==================== CYCLE 34: VOICE AGENT ====================

async function renderVoiceAgent() {
  const el = document.getElementById('page-content');

  // Load initial data
  let settings = {}, agents = [], calls = [], stats = {}, scripts = [];
  try {
    const [sRes, aRes, cRes, stRes] = await Promise.all([
      api('GET', '/api/voice/settings'),
      api('GET', '/api/voice/agents'),
      api('GET', '/api/voice/calls'),
      api('GET', '/api/voice/stats'),
    ]);
    settings = sRes;
    agents = aRes.agents || [];
    calls = cRes.calls || [];
    stats = stRes.stats || {};
  } catch(e) { /* settings not configured yet */ }

  const isConfigured = settings.retell_api_key_set && settings.voice_agent_enabled;
  const callStats = stats.calls || {};

  el.innerHTML = `
    <div style="max-width:1200px;margin:0 auto">
      <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:24px">
        <div>
          <h2 style="margin:0;font-size:24px;font-weight:700">Phone Screening</h2>
          <p style="color:#666;margin:4px 0 0">AI makes phone calls to pre-screen candidates before you spend time interviewing</p>
        </div>
        <div style="display:flex;gap:8px">
          <button onclick="c34ShowSettings()" class="btn btn-secondary" style="font-size:13px">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>
            Settings
          </button>
          ${isConfigured ? `<button onclick="c34ShowNewCall()" class="btn btn-primary" style="font-size:13px;background:#0ace0a;color:#000">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
            New Call
          </button>` : ''}
        </div>
      </div>

      ${!isConfigured ? `
        <div style="background:#fff;border:1px solid #e5e7eb;border-radius:12px;padding:48px;text-align:center;margin-bottom:24px">
          <svg viewBox="0 0 24 24" fill="none" stroke="#0ace0a" stroke-width="1.5" width="48" height="48" style="margin-bottom:16px"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
          <h3 style="margin:0 0 8px;font-size:20px">Set Up Phone Screening</h3>
          <p style="color:#666;margin:0 0 20px;max-width:500px;margin-left:auto;margin-right:auto">Connect your Retell AI account to enable AI-powered outbound calls for scheduling interviews, checking in with candidates, and keeping your pipeline engaged.</p>
          <button onclick="c34ShowSettings()" class="btn btn-primary" style="background:#0ace0a;color:#000;padding:10px 24px;font-size:14px">Connect Retell AI</button>
        </div>
      ` : `
        <!-- Stats Cards -->
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:16px;margin-bottom:24px">
          <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px">
            <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px">Total Calls (30d)</div>
            <div style="font-size:28px;font-weight:700;margin-top:4px">${callStats.total_calls || 0}</div>
          </div>
          <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px">
            <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px">Connected</div>
            <div style="font-size:28px;font-weight:700;color:#0ace0a;margin-top:4px">${callStats.completed_calls || 0}</div>
          </div>
          <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px">
            <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px">Avg Duration</div>
            <div style="font-size:28px;font-weight:700;margin-top:4px">${callStats.avg_duration ? Math.round(callStats.avg_duration) + 's' : '—'}</div>
          </div>
          <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px">
            <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px">Avg Sentiment</div>
            <div style="font-size:28px;font-weight:700;margin-top:4px;color:${(callStats.avg_sentiment||0)>=0?'#0ace0a':'#dc2626'}">${callStats.avg_sentiment ? (callStats.avg_sentiment > 0 ? '+' : '') + callStats.avg_sentiment.toFixed(1) : '—'}</div>
          </div>
          <div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:16px">
            <div style="font-size:12px;color:#666;text-transform:uppercase;letter-spacing:0.5px">At-Risk</div>
            <div style="font-size:28px;font-weight:700;color:#dc2626;margin-top:4px">${(stats.at_risk_candidates||[]).length}</div>
          </div>
        </div>

        <!-- Tabs -->
        <div style="display:flex;gap:0;border-bottom:1px solid #e5e7eb;margin-bottom:20px">
          <button onclick="c34SwitchTab('calls')" class="c34-tab active" id="c34-tab-calls" style="padding:10px 20px;background:none;border:none;border-bottom:2px solid #0ace0a;font-weight:600;cursor:pointer">Call History</button>
          <button onclick="c34SwitchTab('agents')" class="c34-tab" id="c34-tab-agents" style="padding:10px 20px;background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;color:#666">Agents</button>
          <button onclick="c34SwitchTab('scripts')" class="c34-tab" id="c34-tab-scripts" style="padding:10px 20px;background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;color:#666">Scripts</button>
          <button onclick="c34SwitchTab('schedule')" class="c34-tab" id="c34-tab-schedule" style="padding:10px 20px;background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;color:#666">Scheduled</button>
          <button onclick="c34SwitchTab('atrisk')" class="c34-tab" id="c34-tab-atrisk" style="padding:10px 20px;background:none;border:none;border-bottom:2px solid transparent;cursor:pointer;color:#666">At-Risk</button>
        </div>

        <div id="c34-tab-content">
          ${c34RenderCallHistory(calls)}
        </div>
      `}
    </div>
  `;
}

function c34SwitchTab(tab) {
  document.querySelectorAll('.c34-tab').forEach(t => {
    t.style.borderBottomColor = 'transparent';
    t.style.color = '#666';
    t.style.fontWeight = '400';
  });
  const active = document.getElementById('c34-tab-' + tab);
  if (active) {
    active.style.borderBottomColor = '#0ace0a';
    active.style.color = '#000';
    active.style.fontWeight = '600';
  }
  if (tab === 'calls') c34LoadCalls();
  else if (tab === 'agents') c34LoadAgents();
  else if (tab === 'scripts') c34LoadScripts();
  else if (tab === 'schedule') c34LoadSchedule();
  else if (tab === 'atrisk') c34LoadAtRisk();
}

function c34RenderCallHistory(calls) {
  if (!calls || calls.length === 0) {
    return '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:40px;text-align:center;color:#666">No calls yet. Create your first voice agent and start making calls!</div>';
  }
  return `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
    <table style="width:100%;border-collapse:collapse;font-size:13px">
      <thead><tr style="background:#f9fafb;border-bottom:1px solid #e5e7eb">
        <th style="padding:10px 14px;text-align:left;font-weight:600">Candidate</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Agent</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Status</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Duration</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Sentiment</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Outcome</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600">Date</th>
        <th style="padding:10px 14px;text-align:left;font-weight:600"></th>
      </tr></thead>
      <tbody>
        ${calls.map(c => `<tr style="border-bottom:1px solid #f3f4f6;cursor:pointer" onclick="c34ViewCall('${c.id}')">
          <td style="padding:10px 14px;font-weight:500">${c.candidate_name || c.phone_number || '—'}</td>
          <td style="padding:10px 14px;color:#666">${c.agent_name || '—'}</td>
          <td style="padding:10px 14px"><span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;${c34StatusStyle(c.status)}">${c.status || 'unknown'}</span></td>
          <td style="padding:10px 14px">${c.duration_seconds ? c.duration_seconds + 's' : '—'}</td>
          <td style="padding:10px 14px;color:${(c.sentiment_score||0)>=0?'#0ace0a':'#dc2626'}">${c.sentiment_score != null ? (c.sentiment_score > 0 ? '+' : '') + c.sentiment_score.toFixed(1) : '—'}</td>
          <td style="padding:10px 14px">${c.outcome || '—'}</td>
          <td style="padding:10px 14px;color:#666">${c.created_at ? new Date(c.created_at).toLocaleDateString() : '—'}</td>
          <td style="padding:10px 14px"><button class="btn btn-secondary" style="font-size:11px;padding:3px 8px" onclick="event.stopPropagation();c34ViewCall('${c.id}')">View</button></td>
        </tr>`).join('')}
      </tbody>
    </table>
  </div>`;
}

function c34StatusStyle(status) {
  const styles = {
    completed: 'background:#dcfce7;color:#166534',
    in_progress: 'background:#dbeafe;color:#1e40af',
    initiated: 'background:#fef9c3;color:#854d0e',
    queued: 'background:#f3f4f6;color:#374151',
    failed: 'background:#fce4ec;color:#b71c1c',
  };
  return styles[status] || 'background:#f3f4f6;color:#374151';
}

async function c34LoadCalls() {
  const el = document.getElementById('c34-tab-content');
  try {
    const res = await api('GET', '/api/voice/calls');
    el.innerHTML = c34RenderCallHistory(res.calls || []);
  } catch(e) { el.innerHTML = '<p style="color:red">Error loading calls: ' + e.message + '</p>'; }
}

async function c34LoadAgents() {
  const el = document.getElementById('c34-tab-content');
  try {
    const res = await api('GET', '/api/voice/agents');
    const agents = res.agents || [];
    if (agents.length === 0) {
      el.innerHTML = `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:40px;text-align:center">
        <p style="color:#666;margin-bottom:16px">No voice agents configured yet.</p>
        <button onclick="c34CreateAgent()" class="btn btn-primary" style="background:#0ace0a;color:#000">Create Agent</button>
      </div>`;
      return;
    }
    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(320px,1fr));gap:16px">
      ${agents.map(a => `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:20px">
        <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:12px">
          <div>
            <h4 style="margin:0;font-size:16px;font-weight:600">${a.name}</h4>
            <span style="font-size:12px;color:#666">${a.agent_type}</span>
          </div>
          <span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;${a.active ? 'background:#dcfce7;color:#166534' : 'background:#f3f4f6;color:#666'}">${a.active ? 'Active' : 'Inactive'}</span>
        </div>
        <div style="font-size:13px;color:#666;margin-bottom:12px">
          <div><strong>Voice:</strong> ${a.voice_id || 'Default'}</div>
          <div><strong>Phone:</strong> ${a.retell_phone_number || 'Not assigned'}</div>
          <div><strong>Max duration:</strong> ${a.max_call_duration}s</div>
        </div>
        <div style="display:flex;gap:8px">
          <button onclick="c34EditAgent('${a.id}')" class="btn btn-secondary" style="font-size:12px;flex:1">Edit</button>
          <button onclick="c34DeleteAgent('${a.id}')" class="btn btn-secondary" style="font-size:12px;color:#dc2626">Delete</button>
        </div>
      </div>`).join('')}
      <div style="background:#f9fafb;border:2px dashed #d1d5db;border-radius:10px;padding:20px;display:flex;align-items:center;justify-content:center;min-height:160px;cursor:pointer" onclick="c34CreateAgent()">
        <div style="text-align:center;color:#666">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="24" height="24"><line x1="12" y1="5" x2="12" y2="19"/><line x1="5" y1="12" x2="19" y2="12"/></svg>
          <div style="margin-top:8px;font-size:14px;font-weight:500">Create New Agent</div>
        </div>
      </div>
    </div>`;
  } catch(e) { el.innerHTML = '<p style="color:red">Error: ' + e.message + '</p>'; }
}

async function c34LoadScripts() {
  const el = document.getElementById('c34-tab-content');
  try {
    const res = await api('GET', '/api/voice/scripts');
    const scripts = res.scripts || [];
    if (scripts.length === 0) {
      el.innerHTML = `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:40px;text-align:center">
        <p style="color:#666;margin-bottom:16px">No conversation scripts yet. Create an agent first, then add scripts.</p>
      </div>`;
      return;
    }
    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px">
      ${scripts.map(s => {
        const flow = typeof s.conversation_flow === 'string' ? JSON.parse(s.conversation_flow) : s.conversation_flow;
        return `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:20px">
          <div style="display:flex;justify-content:space-between;align-items:start;margin-bottom:8px">
            <h4 style="margin:0;font-size:15px;font-weight:600">${s.name}</h4>
            <span style="font-size:11px;padding:2px 8px;border-radius:10px;background:#f3f4f6">${s.script_type}</span>
          </div>
          <p style="font-size:13px;color:#666;margin:0 0 12px">${s.purpose || 'No description'}</p>
          <div style="font-size:12px;color:#999">${flow && flow.steps ? flow.steps.length + ' steps' : '—'} &bull; Used ${s.use_count || 0} times</div>
        </div>`;
      }).join('')}
    </div>`;
  } catch(e) { el.innerHTML = '<p style="color:red">Error: ' + e.message + '</p>'; }
}

async function c34LoadSchedule() {
  const el = document.getElementById('c34-tab-content');
  try {
    const res = await api('GET', '/api/voice/schedule');
    const scheduled = res.scheduled_calls || [];
    if (scheduled.length === 0) {
      el.innerHTML = '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:40px;text-align:center;color:#666">No scheduled calls.</div>';
      return;
    }
    el.innerHTML = `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f9fafb;border-bottom:1px solid #e5e7eb">
          <th style="padding:10px 14px;text-align:left">Candidate</th>
          <th style="padding:10px 14px;text-align:left">Agent</th>
          <th style="padding:10px 14px;text-align:left">Type</th>
          <th style="padding:10px 14px;text-align:left">Scheduled</th>
          <th style="padding:10px 14px;text-align:left">Attempts</th>
          <th style="padding:10px 14px;text-align:left">Status</th>
        </tr></thead>
        <tbody>
          ${scheduled.map(s => `<tr style="border-bottom:1px solid #f3f4f6">
            <td style="padding:10px 14px;font-weight:500">${s.candidate_name || '—'}</td>
            <td style="padding:10px 14px;color:#666">${s.agent_name || '—'}</td>
            <td style="padding:10px 14px">${s.call_type}</td>
            <td style="padding:10px 14px">${new Date(s.scheduled_at).toLocaleString()}</td>
            <td style="padding:10px 14px">${s.attempt_count}/${s.max_attempts}</td>
            <td style="padding:10px 14px"><span style="padding:2px 8px;border-radius:10px;font-size:11px;${c34StatusStyle(s.status)}">${s.status}</span></td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  } catch(e) { el.innerHTML = '<p style="color:red">Error: ' + e.message + '</p>'; }
}

async function c34LoadAtRisk() {
  const el = document.getElementById('c34-tab-content');
  try {
    const res = await api('GET', '/api/voice/stats');
    const atRisk = (res.stats || {}).at_risk_candidates || [];
    if (atRisk.length === 0) {
      el.innerHTML = '<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;padding:40px;text-align:center;color:#666">No at-risk candidates detected. Great news!</div>';
      return;
    }
    el.innerHTML = `<div style="background:#fff;border:1px solid #e5e7eb;border-radius:10px;overflow:hidden">
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="background:#f9fafb;border-bottom:1px solid #e5e7eb">
          <th style="padding:10px 14px;text-align:left">Candidate</th>
          <th style="padding:10px 14px;text-align:left">Email</th>
          <th style="padding:10px 14px;text-align:left">Pipeline Stage</th>
          <th style="padding:10px 14px;text-align:left">Risk Level</th>
          <th style="padding:10px 14px;text-align:left">Engagement</th>
          <th style="padding:10px 14px;text-align:left">Action</th>
        </tr></thead>
        <tbody>
          ${atRisk.map(c => `<tr style="border-bottom:1px solid #f3f4f6">
            <td style="padding:10px 14px;font-weight:500">${c.first_name} ${c.last_name}</td>
            <td style="padding:10px 14px;color:#666">${c.email || '—'}</td>
            <td style="padding:10px 14px">${c.pipeline_stage || '—'}</td>
            <td style="padding:10px 14px"><span style="padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600;${c.voice_risk_level === 'high' ? 'background:#fce4ec;color:#b71c1c' : 'background:#fef9c3;color:#854d0e'}">${c.voice_risk_level}</span></td>
            <td style="padding:10px 14px">${c.voice_engagement_score ? c.voice_engagement_score.toFixed(0) + '/100' : '—'}</td>
            <td style="padding:10px 14px">
              <button onclick="c34QuickCall('${c.id}')" class="btn btn-primary" style="font-size:11px;padding:3px 10px;background:#0ace0a;color:#000">Call Now</button>
            </td>
          </tr>`).join('')}
        </tbody>
      </table>
    </div>`;
  } catch(e) { el.innerHTML = '<p style="color:red">Error: ' + e.message + '</p>'; }
}

// Settings modal
function c34ShowSettings() {
  const modal = document.createElement('div');
  modal.id = 'c34-settings-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';
  modal.innerHTML = `
    <div style="background:#fff;border-radius:12px;padding:28px;max-width:480px;width:90%;max-height:80vh;overflow-y:auto">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
        <h3 style="margin:0;font-size:18px">Phone Screening Settings</h3>
        <button onclick="document.getElementById('c34-settings-modal').remove()" style="background:none;border:none;font-size:20px;cursor:pointer">&times;</button>
      </div>
      <div style="margin-bottom:16px">
        <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Retell AI API Key</label>
        <input type="password" id="c34-api-key" placeholder="Enter your Retell API key" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;box-sizing:border-box">
        <p style="font-size:11px;color:#666;margin:4px 0 0">Get your API key from <a href="https://www.retellai.com" target="_blank" style="color:#0ace0a">retellai.com</a></p>
      </div>
      <div style="margin-bottom:16px">
        <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Caller ID Phone Number</label>
        <input type="text" id="c34-caller-id" placeholder="+1234567890" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;font-size:14px;box-sizing:border-box">
      </div>
      <div style="margin-bottom:20px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="c34-enabled" style="width:16px;height:16px;accent-color:#0ace0a">
          <span style="font-size:13px;font-weight:600">Turn On Phone Screening</span>
        </label>
      </div>
      <button onclick="c34SaveSettings()" class="btn btn-primary" style="width:100%;background:#0ace0a;color:#000;padding:10px;font-size:14px;font-weight:600">Save Settings</button>
    </div>
  `;
  document.body.appendChild(modal);

  // Load current settings
  api('GET', '/api/voice/settings').then(s => {
    if (s.voice_caller_id) document.getElementById('c34-caller-id').value = s.voice_caller_id;
    document.getElementById('c34-enabled').checked = s.voice_agent_enabled;
  }).catch(() => {});
}

async function c34SaveSettings() {
  const apiKey = document.getElementById('c34-api-key').value;
  const callerId = document.getElementById('c34-caller-id').value;
  const enabled = document.getElementById('c34-enabled').checked;
  const body = { voice_agent_enabled: enabled, voice_caller_id: callerId };
  if (apiKey) body.retell_api_key = apiKey;
  try {
    await api('PUT', '/api/voice/settings', body);
    document.getElementById('c34-settings-modal').remove();
    toast('Voice settings saved!', 'success');
    renderVoiceAgent();
  } catch(e) { toast(e.message, 'error'); }
}

// Create agent
async function c34CreateAgent() {
  const name = prompt('Agent name:', 'Recruiting Agent');
  if (!name) return;
  try {
    await api('POST', '/api/voice/agents', { name, agent_type: 'scheduling' });
    toast('Voice agent created!', 'success');
    c34LoadAgents();
  } catch(e) { toast(e.message, 'error'); }
}

async function c34EditAgent(id) {
  try {
    const res = await api('GET', '/api/voice/agents/' + id);
    const a = res.agent;
    const modal = document.createElement('div');
    modal.id = 'c34-edit-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';
    modal.innerHTML = `
      <div style="background:#fff;border-radius:12px;padding:28px;max-width:520px;width:90%;max-height:80vh;overflow-y:auto">
        <h3 style="margin:0 0 20px;font-size:18px">Edit Agent: ${a.name}</h3>
        <div style="margin-bottom:12px">
          <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Name</label>
          <input type="text" id="c34-edit-name" value="${a.name}" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box">
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Greeting Script</label>
          <textarea id="c34-edit-greeting" rows="3" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box;resize:vertical">${a.greeting_script || ''}</textarea>
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Persona Prompt</label>
          <textarea id="c34-edit-prompt" rows="4" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box;resize:vertical" placeholder="Describe the agent's personality and behavior...">${a.persona_prompt || ''}</textarea>
        </div>
        <div style="margin-bottom:12px">
          <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Max Call Duration (seconds)</label>
          <input type="number" id="c34-edit-duration" value="${a.max_call_duration}" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box">
        </div>
        <div style="display:flex;gap:8px;margin-top:20px">
          <button onclick="c34SaveAgent('${id}')" class="btn btn-primary" style="flex:1;background:#0ace0a;color:#000">Save</button>
          <button onclick="document.getElementById('c34-edit-modal').remove()" class="btn btn-secondary" style="flex:1">Cancel</button>
        </div>
      </div>
    `;
    document.body.appendChild(modal);
  } catch(e) { toast(e.message, 'error'); }
}

async function c34SaveAgent(id) {
  try {
    await api('PUT', '/api/voice/agents/' + id, {
      name: document.getElementById('c34-edit-name').value,
      greeting_script: document.getElementById('c34-edit-greeting').value,
      persona_prompt: document.getElementById('c34-edit-prompt').value,
      max_call_duration: parseInt(document.getElementById('c34-edit-duration').value) || 300,
    });
    document.getElementById('c34-edit-modal').remove();
    toast('Agent updated!', 'success');
    c34LoadAgents();
  } catch(e) { toast(e.message, 'error'); }
}

async function c34DeleteAgent(id) {
  if (!confirm('Delete this voice agent? This cannot be undone.')) return;
  try {
    await api('DELETE', '/api/voice/agents/' + id);
    toast('Agent deleted', 'success');
    c34LoadAgents();
  } catch(e) { toast(e.message, 'error'); }
}

// New call modal
async function c34ShowNewCall() {
  const modal = document.createElement('div');
  modal.id = 'c34-call-modal';
  modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';

  let agentsHtml = '<option value="">Loading...</option>';
  try {
    const res = await api('GET', '/api/voice/agents');
    agentsHtml = (res.agents || []).filter(a => a.active).map(a => `<option value="${a.id}">${a.name}</option>`).join('');
    if (!agentsHtml) agentsHtml = '<option value="">No active agents</option>';
  } catch(e) {}

  let candidatesHtml = '<option value="">Select candidate (optional)</option>';
  try {
    const res = await api('GET', '/api/voice/candidates?consent_only=false');
    candidatesHtml += (res.candidates || []).map(c => `<option value="${c.id}" data-phone="${c.phone || ''}">${c.first_name} ${c.last_name} (${c.phone || 'no phone'})</option>`).join('');
  } catch(e) {}

  modal.innerHTML = `
    <div style="background:#fff;border-radius:12px;padding:28px;max-width:480px;width:90%">
      <h3 style="margin:0 0 20px;font-size:18px">New Voice Call</h3>
      <div style="margin-bottom:12px">
        <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Agent</label>
        <select id="c34-call-agent" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box">${agentsHtml}</select>
      </div>
      <div style="margin-bottom:12px">
        <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Candidate</label>
        <select id="c34-call-candidate" onchange="c34FillPhone()" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box">${candidatesHtml}</select>
      </div>
      <div style="margin-bottom:16px">
        <label style="display:block;font-size:13px;font-weight:600;margin-bottom:4px">Phone Number</label>
        <input type="text" id="c34-call-phone" placeholder="+1234567890" style="width:100%;padding:8px 12px;border:1px solid #d1d5db;border-radius:6px;box-sizing:border-box">
      </div>
      <div style="display:flex;gap:8px">
        <button onclick="c34MakeCall()" class="btn btn-primary" style="flex:1;background:#0ace0a;color:#000">
          <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14" style="display:inline-block;vertical-align:-2px;margin-right:4px"><path d="M22 16.92v3a2 2 0 0 1-2.18 2 19.79 19.79 0 0 1-8.63-3.07 19.5 19.5 0 0 1-6-6 19.79 19.79 0 0 1-3.07-8.67A2 2 0 0 1 4.11 2h3a2 2 0 0 1 2 1.72c.127.96.361 1.903.7 2.81a2 2 0 0 1-.45 2.11L8.09 9.91a16 16 0 0 0 6 6l1.27-1.27a2 2 0 0 1 2.11-.45c.907.339 1.85.573 2.81.7A2 2 0 0 1 22 16.92z"/></svg>
          Call Now
        </button>
        <button onclick="document.getElementById('c34-call-modal').remove()" class="btn btn-secondary" style="flex:1">Cancel</button>
      </div>
    </div>
  `;
  document.body.appendChild(modal);
}

function c34FillPhone() {
  const sel = document.getElementById('c34-call-candidate');
  const opt = sel.options[sel.selectedIndex];
  if (opt && opt.dataset.phone) {
    document.getElementById('c34-call-phone').value = opt.dataset.phone;
  }
}

async function c34MakeCall() {
  const agentId = document.getElementById('c34-call-agent').value;
  const candidateId = document.getElementById('c34-call-candidate').value;
  const phone = document.getElementById('c34-call-phone').value;
  if (!agentId) { toast('Select an agent', 'error'); return; }
  if (!phone) { toast('Enter a phone number', 'error'); return; }
  try {
    await api('POST', '/api/voice/calls', {
      agent_id: agentId,
      candidate_id: candidateId || null,
      phone_number: phone,
    });
    document.getElementById('c34-call-modal').remove();
    toast('Call initiated!', 'success');
    c34LoadCalls();
  } catch(e) { toast(e.message, 'error'); }
}

async function c34QuickCall(candidateId) {
  try {
    const agents = (await api('GET', '/api/voice/agents')).agents || [];
    const active = agents.find(a => a.active);
    if (!active) { toast('No active voice agent. Create one first.', 'error'); return; }
    await api('POST', '/api/voice/calls', { agent_id: active.id, candidate_id: candidateId });
    toast('Call initiated!', 'success');
  } catch(e) { toast(e.message, 'error'); }
}

async function c34ViewCall(callId) {
  try {
    const res = await api('GET', '/api/voice/calls/' + callId);
    const c = res.call;
    const modal = document.createElement('div');
    modal.id = 'c34-view-modal';
    modal.style.cssText = 'position:fixed;inset:0;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;z-index:9999';
    modal.innerHTML = `
      <div style="background:#fff;border-radius:12px;padding:28px;max-width:600px;width:90%;max-height:80vh;overflow-y:auto">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:20px">
          <h3 style="margin:0;font-size:18px">Call Details</h3>
          <button onclick="document.getElementById('c34-view-modal').remove()" style="background:none;border:none;font-size:20px;cursor:pointer">&times;</button>
        </div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:20px">
          <div><strong style="font-size:12px;color:#666">Candidate</strong><div>${c.candidate_name || '—'}</div></div>
          <div><strong style="font-size:12px;color:#666">Phone</strong><div>${c.phone_number}</div></div>
          <div><strong style="font-size:12px;color:#666">Agent</strong><div>${c.agent_name || '—'}</div></div>
          <div><strong style="font-size:12px;color:#666">Status</strong><div><span style="padding:2px 8px;border-radius:10px;font-size:11px;${c34StatusStyle(c.status)}">${c.status}</span></div></div>
          <div><strong style="font-size:12px;color:#666">Duration</strong><div>${c.duration_seconds ? c.duration_seconds + ' seconds' : '—'}</div></div>
          <div><strong style="font-size:12px;color:#666">Sentiment</strong><div style="color:${(c.sentiment_score||0)>=0?'#0ace0a':'#dc2626'}">${c.sentiment_score != null ? c.sentiment_score.toFixed(2) : '—'}</div></div>
          <div><strong style="font-size:12px;color:#666">Outcome</strong><div>${c.outcome || '—'}</div></div>
          <div><strong style="font-size:12px;color:#666">Date</strong><div>${c.created_at ? new Date(c.created_at).toLocaleString() : '—'}</div></div>
        </div>
        ${c.summary ? `<div style="margin-bottom:16px"><strong style="font-size:12px;color:#666">Summary</strong><div style="background:#f9fafb;border-radius:8px;padding:12px;margin-top:4px;font-size:13px">${c.summary}</div></div>` : ''}
        ${c.transcript ? `<div style="margin-bottom:16px"><strong style="font-size:12px;color:#666">Transcript</strong><div style="background:#f9fafb;border-radius:8px;padding:12px;margin-top:4px;font-size:12px;max-height:300px;overflow-y:auto;white-space:pre-wrap">${c.transcript}</div></div>` : ''}
        ${c.recording_url ? `<div style="margin-bottom:16px"><strong style="font-size:12px;color:#666">Recording</strong><div style="margin-top:4px"><audio controls src="${c.recording_url}" style="width:100%"></audio></div></div>` : ''}
        ${c.error_message ? `<div style="background:#fce4ec;border-radius:8px;padding:12px;color:#b71c1c;font-size:13px"><strong>Error:</strong> ${c.error_message}</div>` : ''}
      </div>
    `;
    document.body.appendChild(modal);
  } catch(e) { toast(e.message, 'error'); }
}


// ==================== MOBILE SIDEBAR (Cycle 25) ====================
function toggleMobileSidebar() {
  const sb = document.getElementById('app-sidebar');
  const ov = document.getElementById('sidebar-overlay');
  if (sb && ov) { sb.classList.toggle('open'); ov.classList.toggle('open'); }
}
// Close sidebar on nav click (mobile)
document.querySelectorAll('.nav-item').forEach(el => {
  el.addEventListener('click', () => {
    const sb = document.getElementById('app-sidebar');
    const ov = document.getElementById('sidebar-overlay');
    if (sb) sb.classList.remove('open');
    if (ov) ov.classList.remove('open');
  });
});


// ==================== INIT ====================
loadPage();
loadNotifications();
// Poll for new notifications every 60 seconds
setInterval(loadNotifications, 60000);
