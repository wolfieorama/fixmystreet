#!/usr/bin/perl -w

# index.cgi:
# Main code for Neighbourhood Fix-It
#
# Copyright (c) 2006 UK Citizens Online Democracy. All rights reserved.
# Email: matthew@mysociety.org. WWW: http://www.mysociety.org
#
# $Id: index.cgi,v 1.130 2007-05-14 21:21:44 matthew Exp $

use strict;
require 5.8.0;

# Horrible boilerplate to set up appropriate library paths.
use FindBin;
use lib "$FindBin::Bin/../perllib";
use lib "$FindBin::Bin/../../perllib";
use Error qw(:try);
use File::Slurp;
use Image::Magick;
use LWP::Simple;
use RABX;
use CGI::Carp;
use Digest::MD5 qw(md5_hex);
use URI::Escape;

use Page;
use mySociety::AuthToken;
use mySociety::Config;
use mySociety::DBHandle qw(dbh select_all);
use mySociety::GeoUtil;
use mySociety::Util;
use mySociety::MaPit;
use mySociety::VotingArea;
use mySociety::Web qw(ent NewURL);

BEGIN {
    mySociety::Config::set_file("$FindBin::Bin/../conf/general");
    mySociety::DBHandle::configure(
        Name => mySociety::Config::get('BCI_DB_NAME'),
        User => mySociety::Config::get('BCI_DB_USER'),
        Password => mySociety::Config::get('BCI_DB_PASS'),
        Host => mySociety::Config::get('BCI_DB_HOST', undef),
        Port => mySociety::Config::get('BCI_DB_PORT', undef)
    );

    if (!dbh()->selectrow_array('select secret from secret for update of secret')) {
        local dbh()->{HandleError};
        dbh()->do('insert into secret (secret) values (?)', {}, unpack('h*', mySociety::Util::random_bytes(32)));
    }
    dbh()->commit();
}

# Main code for index.cgi
sub main {
    my $q = shift;

    my $out = '';
    my $title = '';
    my %params;
    if ($q->param('submit_problem')) {
        $title = 'Submitting your problem';
        $out = submit_problem($q);
    } elsif ($q->param('submit_update')) {
        $title = 'Submitting your update';
        $out = submit_update($q);
    } elsif ($q->param('submit_map')) {
        $title = 'Reporting a problem';
        $out = display_form($q);
    } elsif ($q->param('id')) {
        $title = 'Viewing a problem';
        ($out, %params) = display_problem($q);
    } elsif ($q->param('pc') || ($q->param('x') && $q->param('y'))) {
        $title = 'Viewing a location';
        ($out, %params) = display_location($q);
    } else {
        $out = front_page($q);
    }
    print Page::header($q, $title, %params);
    print $out;
    print Page::footer();
    dbh()->rollback();
}
Page::do_fastcgi(\&main);

# Display front page
sub front_page {
    my ($q, $error) = @_;
    my $pc_h = ent($q->param('pc') || '');
    my $out = <<EOF;
<p id="expl">Report, view, or discuss local problems
like graffiti, fly tipping, broken paving slabs, or street lighting</p>
EOF
    $out .= '<p id="error">' . $error . '</p>' if ($error);
    $out .= <<EOF;
<form action="./" method="get" id="postcodeForm">
<label for="pc">Enter a nearby postcode, or street name and area:</label>
&nbsp;<input type="text" name="pc" value="$pc_h" id="pc" size="10" maxlength="200">
&nbsp;<input type="submit" value="Go">
</form>

<p>Reports are sent directly to the local council, apart from a few councils where we&rsquo;re missing details.</p>

<p>Reporting a problem is very simple:</p>

<ol>
<li>Enter a postcode or street name and area;
<li>Locate the problem on a high-scale map;
<li>Enter details of the problem;
<li>Submit to the council.
</ol>

EOF
    return $out;
}

sub submit_update {
    my $q = shift;
    my @vars = qw(id name email update fixed);
    my %input = map { $_ => $q->param($_) || '' } @vars;
    my @errors;
    push(@errors, 'Please enter a message') unless $input{update} =~ /\S/;
    $input{name} = undef unless $input{name} =~ /\S/;
    if ($input{email} !~ /\S/) {
        push(@errors, 'Please enter your email');
    } elsif (!mySociety::Util::is_valid_email($input{email})) {
        push(@errors, 'Please enter a valid email');
    }
    return display_problem($q, @errors) if (@errors);

    my $id = dbh()->selectrow_array("select nextval('comment_id_seq');");
    dbh()->do("insert into comment
        (id, problem_id, name, email, website, text, state, mark_fixed, mark_open)
        values (?, ?, ?, ?, ?, ?, 'unconfirmed', ?, 'f')", {},
        $id, $input{id}, $input{name}, $input{email}, '', $input{update},
        $input{fixed}?'t':'f');
    my %h = ();
    $h{update} = $input{update};
    $h{name} = $input{name} ? $input{name} : "Anonymous";
    $h{url} = mySociety::Config::get('BASE_URL') . '/C/' . mySociety::AuthToken::store('update', $id);
    dbh()->commit();

    my $out = Page::send_email($input{email}, $input{name}, 'update', %h);
    return $out;
}

sub submit_problem {
    my $q = shift;
    my @vars = qw(council title detail name email phone pc easting northing skipped anonymous category);
    my %input = map { $_ => scalar $q->param($_) } @vars;
    my @errors;

    my $fh = $q->upload('photo');
    if ($fh) {
        my $ct = $q->uploadInfo($fh)->{'Content-Type'};
        my $cd = $q->uploadInfo($fh)->{'Content-Disposition'};
        # Must delete photo param, otherwise display functions get confused
        $q->delete('photo');
        push (@errors, 'Please upload a JPEG image only') unless
            ($ct eq 'image/jpeg' || $ct eq 'image/pjpeg');
    }

    push(@errors, 'No council selected') unless ($input{council} && $input{council} =~ /^(?:-1|[\d,]+(?:\|[\d,]+)?)$/);
    push(@errors, 'Please enter a subject') unless $input{title} =~ /\S/;
    push(@errors, 'Please enter some details') unless $input{detail} =~ /\S/;
    push(@errors, 'Please enter your name') unless $input{name} =~ /\S/;
    if ($input{email} !~ /\S/) {
        push(@errors, 'Please enter your email');
    } elsif (!mySociety::Util::is_valid_email($input{email})) {
        push(@errors, 'Please enter a valid email');
    }
    if ($input{category} && $input{category} eq '-- Pick a category --') {
        push (@errors, 'Please choose a category');
        $input{category} = '';
    }
 
    if ($input{easting} && $input{northing}) {
        if ($input{council} =~ /^[\d,]+(\|[\d,]+)?$/) {
            my $no_details = $1 || '';
            my $councils = mySociety::MaPit::get_voting_area_by_location_en($input{easting}, $input{northing}, 'polygon', $mySociety::VotingArea::council_parent_types);
            my %councils = map { $_ => 1 } @$councils;
            my @input_councils = split /,|\|/, $input{council};
            foreach (@input_councils) {
                if (!$councils{$_}) {
                    push(@errors, 'That location is not part of that council');
                    last;
                }
            }

            if ($no_details) {
                $input{council} =~ s/\Q$no_details\E//;
                @input_councils = split /,/, $input{council};
            }

            # Check category here, won't be present if council is -1
            my @valid_councils = @input_councils;
            if ($input{category}) {
                my $categories = select_all("select area_id from contacts
                    where deleted='f' and area_id in ("
                    . $input{council} . ') and category = ?', $input{category});
                push (@errors, 'Please choose a category') unless @$categories;
                @valid_councils = map { $_->{area_id} } @$categories;
                foreach my $c (@valid_councils) {
                    if ($no_details =~ /$c/) {
                        push(@errors, 'We have details for that council');
                        $no_details =~ s/,?$c//;
                    }
                }
            }
            $input{council} = join(',', @valid_councils) . $no_details;
        }
    } elsif ($input{easting} || $input{northing}) {
        push(@errors, 'Somehow, you only have one co-ordinate. Please try again.');
    } else {
        push(@errors, 'You haven\'t specified any sort of co-ordinates. Please try again.');
    }
    
    my $image;
    if ($fh) {
        try {
            $image = Image::Magick->new;
            my $err = $image->Read(file => \*$fh); # Mustn't be stringified
            close $fh;
            throw Error::Simple("read failed: $err") if "$err";
            $err = $image->Scale(geometry => "250x250>");
            throw Error::Simple("resize failed: $err") if "$err";
            my @blobs = $image->ImageToBlob();
            undef $image;
            $image = $blobs[0];
        } catch Error::Simple with {
            my $e = shift;
            push(@errors, "That image doesn't appear to have uploaded correctly ($e), please try again.");
        };
    }

    return display_form($q, @errors) if (@errors);

    delete $input{council} if $input{council} eq '-1';
    my $used_map = $input{skipped} ? 'f' : 't';
    $input{category} = 'Other' unless $input{category};

    my $id = dbh()->selectrow_array("select nextval('problem_id_seq');");
    # This is horrid
    my $s = dbh()->prepare("insert into problem
        (id, postcode, easting, northing, title, detail, name,
         email, phone, photo, state, council, used_map, anonymous, category)
        values
        (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'unconfirmed', ?, ?, ?, ?)");
    $s->bind_param(1, $id);
    $s->bind_param(2, $input{pc});
    $s->bind_param(3, $input{easting});
    $s->bind_param(4, $input{northing});
    $s->bind_param(5, $input{title});
    $s->bind_param(6, $input{detail});
    $s->bind_param(7, $input{name});
    $s->bind_param(8, $input{email});
    $s->bind_param(9, $input{phone});
    $s->bind_param(10, $image, { pg_type => DBD::Pg::PG_BYTEA });
    $s->bind_param(11, $input{council});
    $s->bind_param(12, $used_map);
    $s->bind_param(13, $input{anonymous} ? 'f': 't');
    $s->bind_param(14, $input{category});
    $s->execute();
    my %h = ();
    $h{title} = $input{title};
    $h{detail} = $input{detail};
    $h{name} = $input{name};
    $h{url} = mySociety::Config::get('BASE_URL') . '/P/' . mySociety::AuthToken::store('problem', $id);
    dbh()->commit();

    my $out = Page::send_email($input{email}, $input{name}, 'problem', %h);
    return $out;
}

sub display_form {
    my ($q, @errors) = @_;
    my ($pin_x, $pin_y, $pin_tile_x, $pin_tile_y) = (0,0,0,0);
    my @vars = qw(title detail name email phone pc easting northing x y skipped council anonymous);
    my %input = map { $_ => $q->param($_) || '' } @vars;
    my %input_h = map { $_ => $q->param($_) ? ent($q->param($_)) : '' } @vars;
    my @ps = $q->param;
    foreach (@ps) {
        ($pin_tile_x, $pin_tile_y, $pin_x) = ($1, $2, $q->param($_)) if /^tile_(\d+)\.(\d+)\.x$/;
        $pin_y = $q->param($_) if /\.y$/;
    }
    return display_location($q)
        unless ($pin_x && $pin_y)
            || ($input{easting} && $input{northing})
            || ($input{skipped} && $input{x} && $input{y})
            || ($input{skipped} && $input{pc});

    my $out = '';
    my ($px, $py, $easting, $northing, $island);
    if ($input{skipped}) {
        # Map is being skipped
        if ($input{x} && $input{y}) {
            $easting = Page::tile_to_os($input{x});
            $northing = Page::tile_to_os($input{y});
        } else {
            my ($x, $y, $e, $n, $i, $error) = geocode($input{pc});
            $easting = $e; $northing = $n; $island = $i;
        }
    } elsif ($pin_x && $pin_y) {
        # Map was clicked on
        $pin_x = Page::click_to_tile($pin_tile_x, $pin_x);
        $pin_y = Page::click_to_tile($pin_tile_y, $pin_y, 1);
        $px = Page::tile_to_px($pin_x, $input{x});
        $py = Page::tile_to_px($pin_y, $input{y});
        $easting = Page::tile_to_os($pin_x);
        $northing = Page::tile_to_os($pin_y);
    } else {
        # Normal form submission
        $px = Page::os_to_px($input{easting}, $input{x});
        $py = Page::os_to_px($input{northing}, $input{y});
        $easting = $input_h{easting};
        $northing = $input_h{northing};
    }

    my $all_councils = mySociety::MaPit::get_voting_area_by_location_en($easting, $northing,
        'polygon', $mySociety::VotingArea::council_parent_types);
    my $areas_info = mySociety::MaPit::get_voting_areas_info($all_councils);

    # Look up categories for this council or councils
    my $category = '';
    my %council_ok;
    my $categories = select_all("select area_id, category from contacts
        where deleted='f' and area_id in (" . join(',', @$all_councils) . ')');
    @$categories = sort { $a->{category} cmp $b->{category} } @$categories;
    my @categories;
    foreach (@$categories) {
        $council_ok{$_->{area_id}} = 1;
        next if $_->{category} eq 'Other';
        push @categories, $_->{category};
    }
    if (@categories) {
        @categories = ('-- Pick a category --', @categories, 'Other');
        $category = $q->div($q->label({'for'=>'form_category'}, 'Category:'), 
            $q->popup_menu(-name=>'category', -values=>\@categories,
                -attributes=>{id=>'form_category'})
        );
    }

    my @councils = keys %council_ok;
    my $details;
    if (@councils == @$all_councils) {
        $details = 'all';
    } elsif (@councils == 0) {
        $details = 'none';
    } else {
        $details = 'some';
    }

    if ($input{skipped}) {
        $out .= <<EOF;
<form action="./" method="post">
<input type="hidden" name="pc" value="$input_h{pc}">
<input type="hidden" name="skipped" value="1">
<h1>Reporting a problem</h1>
EOF
    } else {
        my $pins = Page::display_pin($q, $px, $py, 'purple');
        $out .= Page::display_map($q, x => $input{x}, y => $input{y}, type => 2,
            pins => $pins, px => $px, py => $py );
        $out .= '<h1>Reporting a problem</h1>';
        $out .= '<p>You have located the problem at the point marked with a purple pin on the map.
        If this is not the correct location, simply click on the map again.</p>';
    }

    if ($details eq 'all') {
        $out .= '<p>All the details you provide here will be sent to <strong>'
            . join('</strong> or <strong>', map { Page::canonicalise_council($areas_info->{$_}->{name}) } @$all_councils)
            . '</strong>. We show the subject and details of the problem on
            the site, along with your name if you give us permission.</p>';
        $out .= '<input type="hidden" name="council" value="' . join(',',@$all_councils) . '">';
    } elsif ($details eq 'some') {
        my $e = mySociety::Config::get('CONTACT_EMAIL');
        my %councils = map { $_ => 1 } @councils;
        my @missing;
        foreach (@$all_councils) {
            push @missing, $_ unless $councils{$_};
        }
        my $n = @missing;
        my $list = join(' or ', map { Page::canonicalise_council($areas_info->{$_}->{name}) } @missing);
        $out .= '<p>All the details you provide here will be sent to <strong>'
            . join('</strong> or <strong>', map { Page::canonicalise_council($areas_info->{$_}->{name}) } @councils)
            . '</strong>. We show the subject and details of the problem on
            the site, along with your name if you give us permission.</p>';
        $out .= ' We do <strong>not</strong> yet have details for the other council';
        $out .= ($n>1) ? 's that cover' : ' that covers';
        $out .= " this location. You can help us by finding a contact email address for local
problems for $list and emailing it to us at <a href='mailto:$e'>$e</a>.</p>";
        $out .= '<input type="hidden" name="council" value="' . join(',', @councils)
            . '|' . join(',', @missing) . '">';
    } else {
        my $e = mySociety::Config::get('CONTACT_EMAIL');
        my $list = join(' or ', map { Page::canonicalise_council($areas_info->{$_}->{name}) } @$all_councils);
        my $n = @$all_councils;
        $out .= '<p>We do not yet have details for the council';
        $out .= ($n>1) ? 's that cover' : ' that covers';
        $out .= " this location. If you submit a problem here it will be
left on the site, but <strong>not</strong> reported to the council.
You can help us by finding a contact email address for local
problems for $list and emailing it to us at <a href='mailto:$e'>$e</a>.</p>";
        $out .= '<input type="hidden" name="council" value="-1">';
    }
    if ($input{skipped}) {
        $out .= $q->p('Please fill in the form below with details of the problem, and
describe the location as precisely as possible in the details box.');
    } elsif ($details ne 'none') {
        $out .= $q->p('Please fill in details of the problem below. The council won\'t be able
to help unless you leave as much detail as you can, so please describe the
exact location of the problem (e.g. on a wall or the floor), and so on.');
    } else {
        $out .= $q->p('Please fill in details of the problem below.');
    }
    $out .= '<input type="hidden" name="easting" value="' . $easting . '">
<input type="hidden" name="northing" value="' . $northing . '">';

    if (@errors) {
        $out .= '<ul id="error"><li>' . join('</li><li>', @errors) . '</li></ul>';
    }
    my $back = NewURL($q, submit_map => undef, "tile_$pin_tile_x.$pin_tile_y.x" => undef,
        "tile_$pin_tile_x.$pin_tile_y.y" => undef, skipped => undef);
    my $anon = ($input{anonymous}) ? ' checked' : ($input{title} ? '' : ' checked');
    $out .= <<EOF;
<fieldset><legend>Problem details</legend>
$category
<div><label for="form_title">Subject:</label>
<input type="text" value="$input_h{title}" name="title" id="form_title" size="30"></div>
<div><label for="form_detail">Details:</label>
<textarea name="detail" id="form_detail" rows="7" cols="30">$input_h{detail}</textarea></div>
<div><label for="form_name">Name:</label>
<input type="text" value="$input_h{name}" name="name" id="form_name" size="30"></div>
<div class="checkbox"><input type="checkbox" name="anonymous" id="form_anonymous" value="1"$anon>
<label for="form_anonymous">Can we show your name on the site?</label>
<small>(we never show your email address or phone number)</small></div>
<div><label for="form_email">Email:</label>
<input type="text" value="$input_h{email}" name="email" id="form_email" size="30"></div>
<div><label for="form_phone">Phone:</label>
<input type="text" value="$input_h{phone}" name="phone" id="form_phone" size="20">
<small>(optional, so the council can get in touch)</small></div>
<div><label for="form_photo">Photo:</label>
<input type="file" name="photo" id="form_photo"></div>
<div class="checkbox"><input type="submit" name="submit_problem" value="Submit"></div>
</fieldset>

<p align="right"><a href="$back">Back to listings</a></p>
EOF
    $out .= Page::display_map_end(1);
    return $out;
}

sub display_location {
    my ($q, @errors) = @_;

    my @vars = qw(pc x y);
    my %input = map { $_ => $q->param($_) || '' } @vars;
    my %input_h = map { $_ => $q->param($_) ? ent($q->param($_)) : '' } @vars;

    my($error, $easting, $northing, $island);
    my $x = $input{x}; my $y = $input{y};
    $x ||= 0; $x += 0;
    $y ||= 0; $y += 0;
    if (!$x && !$y) {
        try {
            ($x, $y, $easting, $northing, $island, $error) = geocode($input{pc});
        } catch Error::Simple with {
            $error = shift;
        };
    }
    return geocode_choice($error) if (ref($error) eq 'ARRAY');
    return front_page($q, $error) if ($error);

    my ($pins, $current_map, $current, $fixed) = map_pins($q, $x, $y);
    my $out = Page::display_map($q, x => $x, y => $y, type => 1, pins => $pins );
    $out .= '<h1>Click on the map to report a problem</h1>';
    if (@errors) {
        $out .= '<ul id="error"><li>' . join('</li><li>', @errors) . '</li></ul>';
    }
    my $skipurl = NewURL($q, 'submit_map'=>1, skipped=>1);
    $out .= <<EOF;
<p><small>If you cannot see a map &ndash; if you have images turned off,
or are using a text only browser, for example &ndash; and you
wish to report a problem, please
<a href="$skipurl">skip this step</a> and we will ask you
to describe the location of your problem instead.</small></p>
EOF
    $out .= <<EOF;
<div>
<h2>Recent problems reported on this map</h2>
EOF
    my $list = '';
    foreach (@$current_map) {
        $list .= '<li><a href="' . NewURL($q, id=>$_->{id}, x=>undef, y=>undef) . '">';
        $list .= $_->{title};
        $list .= '</a></li>';
    }
    if (@$current_map) {
        $out .= '<ol id="current">' . $list . '</ol>';
    } else {
        $out .= '<p>No problems have been reported yet.</p>';
    }
    $out .= <<EOF;
    <h2>Closest problems within 10km</h2>
    <p><a href="/rss/$x,$y"><img align="right" src="/i/feed.png" width="16" height="16" title="RSS feed of recent local problems" alt="RSS feed" border="0"></a></p>
EOF
    $list = '';
    foreach (@$current) {
        $list .= '<li><a href="' . NewURL($q, id=>$_->{id}, x=>undef, y=>undef) . '">';
        $list .= $_->{title} . ' (c. ' . int($_->{distance}/100+.5)/10 . 'km)';
        $list .= '</a></li>';
    }
    if (@$current) {
        my $list_start = @$current_map + 1;
        $out .= '<ol id="current_near" start="' . $list_start . '">' . $list . '</ol>';
    } else {
        $out .= '<p>No problems have been reported yet.</p>';
    }
    $out .= <<EOF;
    <h2>Recently fixed problems within 10km</h2>
EOF
    $list = '';
    foreach (@$fixed) {
        $list .= '<li><a href="' . NewURL($q, id=>$_->{id}, x=>undef, y=>undef) . '">';
        $list .= $_->{title} . ' (c. ' . int($_->{distance}/100+.5)/10 . 'km)';
        $list .= '</a></li>';
    }
    if (@$fixed) {
        $out .= "<ol>$list</ol>\n";
    } else {
        $out .= '<p>No problems have been fixed yet</p>';
    }
    $out .= '</div>';
    $out .= Page::display_map_end(1);

    my %params = (
        'Recent local problems, Neighbourhood Fix-It' => "/rss/$x,$y"
    );

    return ($out, %params);
}

sub display_problem {
    my ($q, @errors) = @_;

    my @vars = qw(id name email update fixed x y);
    my %input = map { $_ => $q->param($_) || '' } @vars;
    my %input_h = map { $_ => $q->param($_) ? ent($q->param($_)) : '' } @vars;
    $input{x} ||= 0; $input{x} += 0;
    $input{y} ||= 0; $input{y} += 0;

    # Get all information from database
    my $problem = dbh()->selectrow_hashref(
        "select state, easting, northing, title, detail, name, extract(epoch from confirmed) as time, photo, anonymous,
         extract(epoch from whensent-confirmed) as whensent, council, id
         from problem where id=? and state in ('confirmed','fixed', 'hidden')", {}, $input{id});
    return display_location($q, 'Unknown problem ID') unless $problem;
    return front_page($q, 'That problem has been hidden from public view as it contained inappropriate public details') if $problem->{state} eq 'hidden';
    my $x = Page::os_to_tile($problem->{easting});
    my $y = Page::os_to_tile($problem->{northing});
    my $x_tile = $input{x} || int($x);
    my $y_tile = $input{y} || int($y);
    my $px = Page::os_to_px($problem->{easting}, $x_tile);
    my $py = Page::os_to_px($problem->{northing}, $y_tile);

    my $pins = Page::display_pin($q, $px, $py, 'blue');
    my $out = Page::display_map($q, x => $x_tile, y => $y_tile, type => 0,
        pins => $pins, px => $px, py => $py );
    $out .= Page::display_problem_text($q, $problem);

    $out .= $q->p({align=>'right'},
        $q->small($q->a({href => '/contact?id=' . $input{id}}, 'Offensive? Unsuitable? Tell us'))
    );
    my $back = NewURL($q, id=>undef, x=>$x_tile, y=>$y_tile);
    $out .= '<p style="padding-bottom: 0.5em; border-bottom: dotted 1px #999999;" align="right"><a href="' . $back . '">Back to listings</a></p>';

    $out .= '<a href="/rss/'.$input_h{id}.'"><img align="right" src="/i/feed.png" width="16" height="16" title="RSS feed" alt="RSS feed of updates to this problem" border="0" hspace="4"></a> ';
    $out .= '<a id="email_alert" href="/alert?type=updates;id='.$input_h{id}.'"><img src="/i/email.png" width="16" height="16" title="Email alerts" alt="Email alerts of updates to this problem" border="0"></a>';
    $out .= <<EOF;
<form action="alert" method="post" id="email_alert_box">
<p>Receive email when updates are left on this problem</p>
<label class="n" for="alert_email">Email:</label>
<input type="text" name="email" id="alert_email" value="$input_h{email}" size="30">
<input type="hidden" name="id" value="$input_h{id}">
<input type="hidden" name="type" value="updates">
<input type="submit" value="Subscribe">
</form>
EOF

    # Display updates
    my $updates = select_all(
        "select id, name, extract(epoch from created) as created, text, mark_fixed, mark_open
         from comment where problem_id = ? and state='confirmed'
         order by created", $input{id});
    if (@$updates) {
        $out .= '<div id="updates">';
        $out .= '<h2>Updates</h2>';
        foreach my $row (@$updates) {
            $out .= "<div><a name=\"update_$row->{id}\"></a><em>";
            if ($row->{name}) {
                $out .= "Posted by " . ent($row->{name});
            } else {
                $out .= "Posted anonymously";
            }
            $out .= " at " . Page::prettify_epoch($row->{created});
            $out .= ', marked fixed' if ($row->{mark_fixed});
            $out .= ', reopened' if ($row->{mark_open});
            $out .= '</em>';
            $out .= '<br>' . ent($row->{text}) . '</div>';
        }
        $out .= '</div>';
    }
    $out .= '<h2>Provide an update</h2>';
    $out .= $q->p($q->small('Please note that updates are not sent to the council.'));
    if (@errors) {
        $out .= '<ul id="error"><li>' . join('</li><li>', @errors) . '</li></ul>';
    }

    my $fixed = ($input{fixed}) ? ' checked' : '';
    my $fixedline = $problem->{state} eq 'fixed' ? '' : qq{
<div class="checkbox"><input type="checkbox" name="fixed" id="form_fixed" value="1"$fixed>
<label for="form_fixed">This problem has been fixed</label></div>
};
    $out .= <<EOF;
<form method="post" action="./">
<fieldset><legend>Update details</legend>
<input type="hidden" name="submit_update" value="1">
<input type="hidden" name="id" value="$input_h{id}">
<div><label for="form_name">Name:</label>
<input type="text" name="name" id="form_name" value="$input_h{name}" size="30"> (optional)</div>
<div><label for="form_email">Email:</label>
<input type="text" name="email" id="form_email" value="$input_h{email}" size="30"></div>
<div><label for="form_update">Update:</label>
<textarea name="update" id="form_update" rows="7" cols="30">$input_h{update}</textarea></div>
$fixedline
<div class="checkbox"><input type="submit" value="Post"></div>
</fieldset>
</form>
EOF
    $out .= Page::display_map_end(0);

    my %params = (
        'Updates to this problem, Neighbourhood Fix-It' => "/rss/$input_h{id}"
    );
    return ($out, %params);
}

sub map_pins {
    my ($q, $x, $y) = @_;

    my $pins = '';
    my $min_e = Page::tile_to_os($x);
    my $min_n = Page::tile_to_os($y);
    my $mid_e = Page::tile_to_os($x+1);
    my $mid_n = Page::tile_to_os($y+1);
    my $max_e = Page::tile_to_os($x+2);
    my $max_n = Page::tile_to_os($y+2);

    my $current_map = select_all(
        "select id,title,easting,northing from problem where state='confirmed'
         and easting>=? and easting<? and northing>=? and northing<?
         order by created desc limit 9", $min_e, $max_e, $min_n, $max_n);
    my @ids = ();
    my $count_prob = 1;
    my $count_fixed = 1;
    foreach (@$current_map) {
        push(@ids, $_->{id});
        my $px = Page::os_to_px($_->{easting}, $x);
        my $py = Page::os_to_px($_->{northing}, $y);
        $pins .= Page::display_pin($q, $px, $py, 'red', $count_prob++);
    }

    # XXX: Change to only show problems with extract(epoch from ms_current_timestamp()-laststatechange) < 8 weeks
    # And somehow display/link to old problems somewhere else...
    my $current = [];
    if (@$current_map < 9) {
        my $limit = 9 - @$current_map;
        $current = select_all(
            "select id, title, easting, northing, distance
                from problem_find_nearby(?, ?, 10) as nearby, problem
                where nearby.problem_id = problem.id
                and state = 'confirmed'" . (@ids ? ' and id not in (' . join(',' , @ids) . ')' : '') . "
             order by distance, created desc limit $limit", $mid_e, $mid_n);
        foreach (@$current) {
            my $px = Page::os_to_px($_->{easting}, $x);
            my $py = Page::os_to_px($_->{northing}, $y);
            $pins .= Page::display_pin($q, $px, $py, 'red', $count_prob++);
        }
    }
    my $fixed = select_all(
        "select id, title, easting, northing, distance
            from problem_find_nearby(?, ?, 10) as nearby, problem
            where nearby.problem_id = problem.id and state='fixed'
         order by created desc limit 9", $mid_e, $mid_n);
    foreach (@$fixed) {
        my $px = Page::os_to_px($_->{easting}, $x);
        my $py = Page::os_to_px($_->{northing}, $y);
        $pins .= Page::display_pin($q, $px, $py, 'green', $count_fixed++);
    }
    return ($pins, $current_map, $current, $fixed);
}

sub geocode_choice {
    my $choices = shift;
    my $out = '<p>We found more than one match for that location:</p> <ul>';
    foreach my $choice (@$choices) {
        my $qs = $choice->[0];
        my $text = $choice->[1];
        $text =~ s/<\/?(?:b|i)>//g;
        $text =~ s/, United Kingdom//;
        $qs =~ s/,\+United\+Kingdom//;
        $out .= '<li><a href="/?pc=' . $qs . '">' . $text . "</a></li>\n";
    }
    $out .= '</ul>';
    return $out;
}

sub geocode {
    my ($s) = @_;
    my ($x, $y, $easting, $northing, $island, $error);
    if (mySociety::Util::is_valid_postcode($s)) {
        try {
            my $location = mySociety::MaPit::get_location($s);
            $island = $location->{coordsyst};
            throw RABX::Error("We do not cover Northern Ireland, I'm afraid, as our licence doesn't include any maps for the region.") if $island eq 'I';
            $easting = $location->{easting};
            $northing = $location->{northing};
            my $xx = Page::os_to_tile($easting);
            my $yy = Page::os_to_tile($northing);
            $x = int($xx);
            $y = int($yy);
            $x -= 1 if ($xx - $x < 0.5);
            $y -= 1 if ($yy - $y < 0.5);
        } catch RABX::Error with {
            my $e = shift;
            if ($e->value() && ($e->value() == mySociety::MaPit::BAD_POSTCODE
               || $e->value() == mySociety::MaPit::POSTCODE_NOT_FOUND)) {
                $error = 'That postcode was not recognised, sorry.';
            } else {
                $error = $e;
            }
        }
    } else {
        ($x, $y, $easting, $northing, $error) = geocode_string($s);
    }
    return ($x, $y, $easting, $northing, $island, $error);
}

sub geocode_string {
    my $s = shift;
    $s = lc($s);
    $s =~ s/[^-&0-9a-z ']/ /g;
    $s = uri_escape($s);
    $s =~ s/%20/+/g;
    my $url = 'http://maps.google.co.uk/maps?output=js&q=' . $s;
    my $cache_dir = mySociety::Config::get('GEO_CACHE');
    my $cache_file = $cache_dir . md5_hex($url);
    my ($js, $error, $x, $y, $easting, $northing);
    if (-s $cache_file) {
        $js = File::Slurp::read_file($cache_file);
    } else {
        $url .= ',+United+Kingdom' unless $url =~ /United\+Kingdom$/;
        $js = LWP::Simple::get($url);
        File::Slurp::write_file($cache_file, $js) if $js;
    }
    if (!$js) {
        $error = 'Sorry, we had a problem parsing that location. Please try again.';
    } elsif ($js =~ /suggest noprint/ && $js =~ /We could not understand/) {
        $error = $1;
    } elsif ($js =~ /suggest noprint/) {
        while ($js =~ /<div class=\\042ref\\042><a href=\\042\/maps\?q=(.*?)&.*?>(.*?)<\/a><\/div>/g) {
            push (@$error, [ $1, $2 ]);
        }
        $error = 'We could not understand that location.' unless $error;
    } elsif ($js =~ /BT\d/) {
        # Northern Ireland, hopefully
        $error = "We do not cover Northern Ireland, I'm afraid, as our licence doesn't include any maps for the region.";
    } else {
        $js =~ /center: {lat: (.*?),lng: (.*?)}/;
        my $lat = $1; my $lon = $2;
        ($easting,$northing) = mySociety::GeoUtil::wgs84_to_national_grid($lat, $lon, 'G');
        $x = int(Page::os_to_tile($easting))-1;
        $y = int(Page::os_to_tile($northing))-1;
    }
    return ($x, $y, $easting, $northing, $error);
}

