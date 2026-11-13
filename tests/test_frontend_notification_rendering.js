/**
 * Test suite to verify frontend notification rendering, edge-case resilience,
 * missingGroup reference safety, and tab rendering.
 */
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

// Read frontend/index.html
const html = fs.readFileSync('frontend/index.html', 'utf8');

// Set up mock DOM environment
const mockDOM = {
    elements: {},
    addEventListener: () => {},
    removeEventListener: () => {},
    getElementById(id) {
        if (!this.elements[id]) {
            this.elements[id] = {
                id,
                innerHTML: '',
                innerText: '',
                className: '',
                style: {},
                classList: {
                    add: () => {},
                    remove: () => {},
                    toggle: () => {}
                },
                disabled: false,
                checked: false,
                dispatchEvent: () => {}
            };
        }
        return this.elements[id];
    },
    querySelectorAll(selector) {
        return [];
    }
};

const sandbox = {
    document: mockDOM,
    window: {
        scrollTo: () => {}
    },
    console: {
        log: () => {},
        error: (...args) => console.log('   [Simulated handled error]:', ...args),
        warn: () => {}
    },
    Set: Set,
    Array: Array,
    Number: Number,
    String: String,
    Boolean: Boolean,
    Date: Date,
    JSON: JSON,
    encodeURIComponent: encodeURIComponent,
    decodeURIComponent: decodeURIComponent,
    API_BASE: 'http://localhost:8000/api'
};

vm.createContext(sandbox);

// Extract the script tag that contains renderNotifications
const scripts = html.match(/<script[\s\S]*?<\/script>/gi) || [];
const mainScript = scripts.find(s => s.includes('function renderNotifications'));
assert(mainScript, 'Main script containing renderNotifications must exist');
const cleanScript = mainScript.replace(/<\/?script[^>]*>/gi, '');

// Execute script in sandbox
vm.runInContext(cleanScript, sandbox);

console.log('✓ Script evaluated successfully in sandbox');

// Verify functions exist
assert(typeof sandbox.renderNotifications === 'function', 'renderNotifications should be defined');
assert(typeof sandbox.isNotificationSelectable === 'function', 'isNotificationSelectable should be defined');
assert(typeof sandbox.escapeHtml === 'function', 'escapeHtml should be defined');

const listContainer = mockDOM.getElementById('notification-list');

// -------------------------------------------------------------------------
// Test Case 1: Empty notifications array []
// -------------------------------------------------------------------------
console.log('Test 1: Empty notifications array []');
sandbox.renderNotifications([]);
assert(listContainer.innerHTML.includes('Catalog is Up to Date'), 'Empty list must render "Catalog is Up to Date"');
console.log('✓ Test 1 passed');

// -------------------------------------------------------------------------
// Test Case 2: Null / undefined notifications
// -------------------------------------------------------------------------
console.log('Test 2: Null / undefined notifications');
sandbox.renderNotifications(null);
assert(listContainer.innerHTML.includes('Catalog is Up to Date'), 'Null input must render "Catalog is Up to Date" without crashing');
sandbox.renderNotifications(undefined);
assert(listContainer.innerHTML.includes('Catalog is Up to Date'), 'Undefined input must render "Catalog is Up to Date" without crashing');
console.log('✓ Test 2 passed');

// -------------------------------------------------------------------------
// Test Case 3: All songs already in MusicData (missingCount = 0)
// This was the EXACT case that threw "missingGroup is not defined"!
// -------------------------------------------------------------------------
console.log('Test 3: All songs already in MusicData (missingGroup = [], verifyGroup = [])');
const allExistingNotif = {
    id: 101,
    album_name: 'Existing Hits',
    language: 'Telugu',
    notification_type: 'ALBUM_UPDATED',
    year: 2026,
    songs: [
        { song_title: 'Track 1', exists_in_catalog: true },
        { song_title: 'Track 2', exists_in_catalog: true }
    ]
};
sandbox.renderNotifications([allExistingNotif]);
assert(listContainer.innerHTML.includes('Existing in MusicData (2)'), 'Must render existing tracks group');
assert(listContainer.innerHTML.includes('All songs in MusicData'), 'Must render "All songs in MusicData" status badge');
console.log('✓ Test 3 passed');

// -------------------------------------------------------------------------
// Test Case 4: Notification with missing songs and authorized download
// -------------------------------------------------------------------------
console.log('Test 4: Notification with missing songs ready for download');
const missingSongsNotif = {
    id: 102,
    album_name: 'Brand New Album',
    language: 'Telugu',
    notification_type: 'NEW_ALBUM',
    year: 2026,
    songs: [
        {
            song_title: 'Song One',
            exists_in_catalog: false,
            source_audio_url: 'https://naasongs.com.co/s1.mp3',
            download_authorized: true,
            link_available: true
        },
        {
            song_title: 'Song Two',
            exists_in_catalog: false,
            source_audio_url: 'https://naasongs.com.co/s2.mp3',
            download_authorized: true,
            link_available: true
        }
    ]
};
sandbox.renderNotifications([missingSongsNotif]);
assert(listContainer.innerHTML.includes('Missing Tracks (2)'), 'Must render missing tracks header');
assert(listContainer.innerHTML.includes('Download All (2)'), 'Must render Batch Download All button with count 2');
console.log('✓ Test 4 passed');

// -------------------------------------------------------------------------
// Test Case 5: Notification with missing songs but download unavailable
// -------------------------------------------------------------------------
console.log('Test 5: Missing songs but download unavailable');
const unavailNotif = {
    id: 103,
    album_name: 'Upcoming Single',
    language: 'Tamil',
    notification_type: 'NEW_SONG',
    year: 2026,
    songs: [
        {
            song_title: 'Upcoming Track',
            exists_in_catalog: false,
            source_audio_url: '',
            download_authorized: false,
            link_available: false
        }
    ]
};
sandbox.renderNotifications([unavailNotif]);
assert(listContainer.innerHTML.includes('Download unavailable on source'), 'Must indicate download unavailable on source');
console.log('✓ Test 5 passed');

// -------------------------------------------------------------------------
// Test Case 6: Notification with version verification needed
// -------------------------------------------------------------------------
console.log('Test 6: Version verification needed (POSSIBLE_VERSION_MATCH)');
const verifyNotif = {
    id: 104,
    album_name: 'Version Album',
    language: 'Hindi',
    notification_type: 'MISSING_SONGS',
    year: 2026,
    songs: [
        {
            song_title: 'Promo Song',
            match_classification: 'POSSIBLE_VERSION_MATCH',
            catalog_song_title: 'Promo Song (Full)',
            exists_in_catalog: false,
            source_audio_url: 'https://naasongs.com.co/promo.mp3'
        }
    ]
};
sandbox.renderNotifications([verifyNotif]);
assert(listContainer.innerHTML.includes('Version Verification Needed (1)'), 'Must render version verification group');
assert(listContainer.innerHTML.includes('Version verification needed'), 'Must display version verification badge');
console.log('✓ Test 6 passed');

// -------------------------------------------------------------------------
// Test Case 7: Notification with empty songs array [] and SCAN_FAILED
// -------------------------------------------------------------------------
console.log('Test 7: Empty songs array []');
const emptySongsNotif = {
    id: 105,
    album_name: 'Failed Scan Album',
    language: 'Kannada',
    notification_type: 'SCAN_FAILED',
    year: 2026,
    message: 'Page failed to parse',
    songs: []
};
sandbox.renderNotifications([emptySongsNotif]);
assert(listContainer.innerHTML.includes('Scan failed on source'), 'Must render scan failed message without throwing error');
console.log('✓ Test 7 passed');

// -------------------------------------------------------------------------
// Test Case 8: Notification with null songs field
// -------------------------------------------------------------------------
console.log('Test 8: Notification with null songs field');
const nullSongsNotif = {
    id: 106,
    album_name: 'No Songs Album',
    language: 'Malayalam',
    notification_type: 'NEW_ALBUM',
    year: 2026,
    songs: null
};
sandbox.renderNotifications([nullSongsNotif]);
assert(listContainer.innerHTML.includes('No tracks available'), 'Must render "No tracks available" without throwing error');
console.log('✓ Test 8 passed');

// -------------------------------------------------------------------------
// Test Case 9: Fault-tolerance - Malformed notification does NOT break others
// -------------------------------------------------------------------------
console.log('Test 9: Malformed notification isolation');
const corruptedNotif = {
    id: 107,
    album_name: 'Corrupted Notification',
    get songs() {
        throw new Error('Simulated database corruption in songs column');
    }
};
const goodNotif = {
    id: 108,
    album_name: 'Valid Subsequent Album',
    language: 'Telugu',
    notification_type: 'NEW_ALBUM',
    year: 2026,
    songs: [
        { song_title: 'Good Track', exists_in_catalog: true }
    ]
};
sandbox.renderNotifications([corruptedNotif, goodNotif]);
assert(listContainer.innerHTML.includes('Corrupted Notification'), 'Must render fallback card for corrupted notification');
assert(listContainer.innerHTML.includes('Valid Subsequent Album'), 'Valid subsequent notification MUST be rendered successfully');
console.log('✓ Test 9 passed: Malformed notification safely isolated!');

// -------------------------------------------------------------------------
// Test Case 10: Multi-language badge styles
// -------------------------------------------------------------------------
console.log('Test 10: Language badge styles');
const langs = ['Telugu', 'Tamil', 'Hindi', 'Kannada', 'Malayalam'];
const langNotifs = langs.map((lang, idx) => ({
    id: 200 + idx,
    album_name: `${lang} Release`,
    language: lang,
    notification_type: 'NEW_ALBUM',
    songs: []
}));
sandbox.renderNotifications(langNotifs);
langs.forEach(lang => {
    assert(listContainer.innerHTML.includes(`${lang} Release`), `Must render ${lang} notification`);
});
console.log('✓ Test 10 passed');

console.log('\n======================================================');
console.log('ALL 10 FRONTEND NOTIFICATION TESTS PASSED SUCCESSFULLY!');
console.log('======================================================\n');
