import { generatePiAudio } from './pi_capture.mjs';

// Deliberately fixed and public: no bookmark, source article or personal data.
try {
  const result = await generatePiAudio({
    title: '', includeTitle: false, mode: 'punctuation_only',
    text: 'This is a short audio test. The sky is blue, and the morning is quiet. Clear speech helps us understand each sentence.',
  }, process.argv[2], { requireGuest: true });
  process.stdout.write(JSON.stringify({ status: result.status, guestVerified: result.guestVerified }) + '\n');
} catch (error) {
  // The caller retains private diagnostic files, never raw browser errors.
  const code = ['PI_ACCESS_CHALLENGE', 'PI_NEEDS_ONBOARDING', 'PI_GUEST_NOT_VERIFIED'].includes(error.message)
    ? error.message : 'guest_audio_unavailable';
  process.stdout.write(JSON.stringify({ status: 'failed', code }) + '\n');
  process.exitCode = 1;
}
