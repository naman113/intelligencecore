import { useNavigate } from 'react-router-dom'
import { Interview as InterviewPanel } from '../components/Interview'
import { useOwed } from '../lib/owed'
import { PageHeader } from '../components/ui'

/* The first thing to do on a fresh install. Everything downstream — triage,
 * playbooks, what counts as urgent — reads the business profile this produces. */
export default function Interview() {
  const owed = useOwed()
  const navigate = useNavigate()

  return (
    <>
      <PageHeader
        title="Onboarding interview"
        lede="It starts knowing nothing about your business, and the WhatsApp connector only sees messages from the moment it was linked. This conversation is where it learns the rest."
        actions={
          <button className="btn btn-quiet" onClick={() => navigate('/')}>
            Back to Today
          </button>
        }
      />

      <div className="panel" style={{ maxWidth: 900 }}>
        <InterviewPanel kind="business" onComplete={() => void owed.reload()} />
      </div>
    </>
  )
}
