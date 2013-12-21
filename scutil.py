#!/usr/bin/python
import os.path
from starcluster.config import StarClusterConfig
from starcluster.cluster import ClusterManager
from starcluster import static
from pprint import pprint
from boto.exception import EC2ResponseError
from boto.ec2.connection import EC2Connection
import boto.ec2
import time
from datetime import datetime, timedelta
import dateutil.parser
import ConfigParser
import re

def cpVolume(volume_id, region_to, zone_to, region_from=None):
    if region_from is None:
        region_from = region_to
    conn = boto.ec2.connect_to_region(region_from)
    volumes = conn.get_all_volumes([volume_id])
    description = 'Created for copy of %s from %s to %s on %s' % (volume_id, region_from, zone_to, datetime.today().isoformat(' ') )
    print "Initiating", description
    vol = volumes[0]
    snap = vol.create_snapshot( description )
    
    while snap.status != u'completed':
        time.sleep(30)
        snap.update()
        print "Snapshot status:",snap.status
       
    if 'Name' in vol.tags:
        snap.add_tag('Name', vol.tags['Name'] + ' copy')
    snap.add_tag('Source Volume', vol.id)
    snap.add_tag('Source Zone', vol.type)

    print snap.__dict__
    if region_from != region_to:
        conn = boto.ec2.connect_to_region(region_to)

    new_snap_id = conn.copy_snapshot( source_region=region_from, source_snapshot_id=snap.id, description=description)
    new_snap = conn.get_all_snapshots( [new_snap_id])[0]
    while new_snap.status != u'completed':
        time.sleep(30)
        new_snap.update()
        print "New Snapshot Status:", new_snap.status
    if 'Name' in vol.tags:
        new_snap.add_tag('Name', vol.tags['Name'] + ' copy')
    new_snap.add_tag('Source ID',snap.id)
    new_snap.add_tag('Source Region', region_from)


    print "Creating new volume"    
    new_vol = new_snap.create_volume(zone_to, size=vol.size, volume_type=vol.type, iops=vol.iops)
    while new_vol.status != u'available':
        time.sleep(30)
        print "New Volume Status", new_vol.update()
        
    if 'Name' in vol.tags:
        new_vol.add_tag('Name',vol.tags['Name'][:10] + '-' + zone_to)
    new_vol.add_tag('Source ID', vol.id)
    new_vol.add_tag('Source Zone', vol.zone)
    return new_vol.id


def cpAMI( dest_region, source_region, source_image_id):
    print  dest_region, source_region, source_image_id
    if source_region == dest_region:
        return  source_image_id
    image = amiExists(dest_region, source_image_id)
    
    if image is not None:
        print "AMI exists in %s" % dest_region
        return image.id

    conn=boto.ec2.connect_to_region(source_region)
    image = conn.get_all_images([source_image_id])[0]
    
    conn = boto.ec2.connect_to_region(dest_region)
    name = image.name[:20] + " copy"
    description = "Copy of %s from region %s on %s" % (source_image_id, source_region,  datetime.today().isoformat(' '))
    print description
    
    res = conn.copy_image(source_region=source_region, source_image_id=source_image_id, name=name, description=description)
    print "Created Image:%s"%res.image_id
    image = conn.get_all_images([res.image_id])[0]
    while image.update() != 'available':
        time.sleep(30)
        print "AMI creation state:", image.state
    print "Image ready"
    image.add_tag('Source AMI', source_image_id)
    image.add_tag('Source Region', source_region)
    image.update()
    return image.id

def amiExists( dest_region, source_image_id ):
    conn=boto.ec2.connect_to_region(dest_region)
    if conn is None:
        raise Exception('Region %s, not found' % dest_region)
    images = conn.get_all_images(owners=['self'])

    for image in images:
        if 'Source AMI' in image.tags and image.tags['Source AMI'] == source_image_id:
            print "AMI source copy already exists in this region"
            return image
    return None

def readLog(logfile=static.HISTORY_FILE,tag=None):
    temp = []
    with open(logfile,'r') as lf:
        for line in lf:
            temp.append(line.strip().split('\t'))
    temp.reverse()
    if tag is None:
        return temp
    else:
        for region,template,ctag,_ in temp:
            if ctag==tag:
                return region,template

def getSpotPrices(instance_type, max_times=10):
    result = {}
    for r in boto.ec2.regions():
        try:
            conn = boto.ec2.connect_to_region(r.name)
            for z in  conn.get_all_zones():
                end = datetime.utcnow().isoformat()
                start = (datetime.utcnow() - timedelta(days=1)).isoformat()

                sph =  conn.get_spot_price_history(start_time=start,instance_type=instance_type, availability_zone=z.name)
                if len(sph) > 0:
                    sph = sph[:max_times]
                    final =  dateutil.parser.parse(sph[0].timestamp)
                    hdr = []
                    cost = []    
                    for s in sph:
                        a = dateutil.parser.parse(s.timestamp)
                        if final-a < timedelta(days=1):
                            hdr.append(str((final - a)).split(':')[0] +':' + str((final - a)).split(':')[1])
                            cost.append(str(s.price))
                    result[z.name] = {}
                    result[z.name]['print'] = '\n'.join([z.name, '\t'+ '\t'.join(hdr), '\t'+ '\t'.join(cost)])
                    result[z.name]['current'] = float(cost[0])
        except EC2ResponseError as e:
            pass
    return result  

def checkPrices(ctype=None, cluster_tag=None, max_times=10):
    """
    Checks the spot prices
    no args compares currently running clusters to possible spot prices
    ctype checks for a certain type of image i.e. 'm1.xlarge' and returns prices in order of cheapest to most expensive
    cluster_tag checks only for one running cluster
    """
    assert ctype is None or cluster_tag is None, "you cannot specify a cluster and an image type."
    if ctype==None:
        cfg = StarClusterConfig().load()
        cm = ClusterManager(cfg)
        for cluster in cm.get_clusters():
            if cluster_tag is not None and  cluster.cluster_tag != cluster_tag:
                continue
            region, template = readLog(tag = cluster.cluster_tag)
            t = "%s - of type(%s) in zone(%s) with bid(%s)"% (cluster.cluster_tag,  cluster.master_instance_type, cluster._nodes[0].placement,cluster.spot_bid)
            print "="*len(t)
            print t
           
            sp = getSpotPrices( cluster.master_instance_type, max_times=max_times )
            
            print sp[cluster._nodes[0].placement]['print']
            print "="*len(t)
            print 
            print
            print "Better/equal prices @"
            curr = sp[cluster._nodes[0].placement]['current']
            for k,v in sp.iteritems():
                if curr >= v['current'] and k != cluster._nodes[0].placement:
                    print v['print']
    else:
    #print cluster.__dict__
        sp = getSpotPrices( ctype, max_times=max_times )
        a = [(p['current'],k) for k,p in sp.iteritems()]
        a.sort()
        print "type(%s)  from cheapest to most expensive"%ctype
        for _,k in a:
            print sp[k]['print']

def getSpotStatus():
    ignore_states = ['cancelled', 'active']
    for r in boto.ec2.regions():
        if r.name != 'us-gov-west-1':
            conn = boto.ec2.connect_to_region(r.name)
            sir = conn.get_all_spot_instance_requests()
            if len(sir) > 0:
                print "Spot Instance requests in ",r.name
            
            running = []
            cancelled = []
            for s in sir:
                if s.state not in ignore_states:
                    print "\tpending id:",s.id
                    print "\t\tstate:",s.state
                    print "\t\tstatus:",s.status.code
                    print "\t\tzone:", s.launch_specification.placement
                    print "\t\ttype:", s.launch_specification.instance_type
                elif s.state=='active':
                    running.append(s.id+'('+s.launch_specification.instance_type+')['+s.launch_specification.placement[-2:]+']' )
                elif s.state=='cancelled':
                        cancelled.append(s.id+'('+s.launch_specification.instance_type+')['+s.launch_specification.placement[-2:]+']' )
            if len(running) > 0:
               print "\trunning instances:", ', '.join(running)
        
            if len(cancelled) > 0:
               print "\tcancelled instances:", ', '.join(cancelled)


def cloneCluster(new_zone, cluster_tag=None, template=None):
    assert cluster_tag is not None or template is not None, "You must provide a cluster template or a cluster tag"
    #get full config
    cfg = StarClusterConfig().load()
    cm = ClusterManager(cfg)
    dest_region = new_zone[:-1]
    if cluster_tag is not None: 
        for cluster in cm.get_clusters():
            if cluster.cluster_tag == cluster_tag:
                region, template = readLog(tag = cluster.cluster_tag)
                break

    cluster_sect, volume_sect = getClusterConfig(template, cfg)
    #get autogenerated portion of config
    auto_config = ConfigParser.RawConfigParser()
    auto_config.read(os.path.expanduser('~/.starcluster/cfgs/auto-gen.cfg'))
    new_cluster_sect = []
    new_volume_sect = []
    for key,value in cluster_sect:
        if key == 'node_image_id' or key=='master_image_id':
            source_region = regionFromAMI(value )
            if source_region is None:
                raise Exception("Cannot find %s  in any region." % value)
            new_ami = cpAMI( dest_region=dest_region, source_region=source_region, source_image_id=value)
           
            print "NA: ", new_ami 
            new_cluster_sect.append((key,new_ami))
        elif key == 'extends':
            if value[:4] != 'base':
                print "*"*50
                print "ALERT,%s extends non base cluster, CHECK CONFIG"%template
                print "*"*50

            new_cluster_sect.append((key,'base-%s'%dest_region))
        elif key == 'volumes':
            value = removeRegion(value)
            volume_sec_name = 'volume %s-%s' % (value, new_zone)
            counter = 0
            while auto_config.has_section(volume_sec_name):
                volume_sec_name = 'volume %s-%s-%02d' % (value, new_zone,counter)
                counter += 1

            for k,v in volume_sect:
                if k == 'volume_id':
                    region_from, zone_from = regionFromVolume( v )
                    if region_from is None:
                        raise Exception("Cannot find %s  in any region." % v)
                    new_vol_id = cpVolume(v, new_zone[:-1], new_zone, region_from=region_from)
                    new_volume_sect.append((k, new_vol_id))
                else:
                    new_volume_sect.append((k,v))
            new_cluster_sect.append((key, volume_sec_name.split(' ')[1]))
        else:
            new_cluster_sect.append((key,value))
            
    cluster_sec_name = 'cluster %s-%s' % (template, new_zone)
    counter = 0
    while auto_config.has_section(cluster_sec_name):
        cluster_sec_name = 'cluster %s-%s-%02d' % (template, new_zone, counter)
        counter += 1

    auto_config.add_section(cluster_sec_name)
    auto_config.add_section(volume_sec_name)
    for k,v in  new_cluster_sect:
        auto_config.set(cluster_sec_name, k,v)

    for k,v in  new_volume_sect:
        auto_config.set(volume_sec_name, k,v)

    with open(os.path.expanduser('~/.starcluster/cfgs/auto-gen.cfg'), 'wb') as configfile:
        auto_config.write(configfile)

    print "starcluster -r %s start --force-spot-master -b <my-bid> -c %s my-cluster"%(new_zone[:-1], cluster_sec_name.split(' ')[1])



def getClusterConfig(cluster_template, cfg):
    cluster_sect = cfg._config.items('cluster %s' % cluster_template)
    for name, value in cluster_sect:
        if name == 'volumes':
            volume_sect = cfg._config.items('volume %s'% value)
            
    return cluster_sect, volume_sect

def removeRegion( astring):
    for r in boto.ec2.regions():
        if r.name != 'us-gov-west-1':
            conn = boto.ec2.connect_to_region(r.name)
            for z in conn.get_all_zones():
                new_string = re.sub( r'-?'+z.name+'-?', '', astring)
                if len(new_string) < len(astring):
                    return new_string
            new_string = re.sub( r'-?'+r.name+'-?', '', astring)
            if len(new_string) < len(astring):
                return new_string
    return astring 

def regionFromAMI( ami_id ):
    
    for r in boto.ec2.regions():
        if r.name != 'us-gov-west-1':
            conn = boto.ec2.connect_to_region(r.name)
            try:
                image = conn.get_all_images([ami_id])
                if len(image) > 0:
                    return r.name
            except:
                pass
    return None

def regionFromVolume( volume_id ):
    
    for r in boto.ec2.regions():
        if r.name != 'us-gov-west-1':
            conn = boto.ec2.connect_to_region(r.name)
            try:
                volumes = conn.get_all_volumes([volume_id])
                if len(volumes) > 0:
                    return r.name, volumes[0].zone
            except:
                pass
    return None,None

if __name__ == "__main__":
    print regionFromAMI( ami_id='ami-899d49e0' )
    print regionFromVolume( volume_id='vol-c0b217bf' )
    
    cloneCluster('us-east-1d', cluster_tag='test-cluster')
    assert 1==0           
    checkPrices()
    checkPrices(ctype='cg1.4xlarge')
    checkPrices(cluster_tag='test-cluster')
    """
    print amiExists( 'eu-west-1',  'ami-5ffbba36')

    volume_id = cpVolume('vol-02a20e56', 'us-east-1', 'us-east-1a', 'us-west-1')
    new_id = cpAMI('us-west-1', 'us-east-1', 'ami-5ffbba36')
    """




    cluster_cfg = 'gpu-sub-cluster-east-1c'
    cfg = StarClusterConfig().load()
    print cfg._config.sections()
    cluster_sect = cfg._config.items('cluster %s' % cluster_cfg)
    print cluster_sect
    for name, value in cluster_sect:
        if name == 'volumes':
            volume_sect = cfg._config.items('volume %s'% value)
            
            print volume_sect


    """
    with open('test.cfg','w') as test:
        cfg._config.write(test)
        

    #print cfg.get_cluster_names().keys()
    cm = ClusterManager(cfg)
    for cluster in cm.get_clusters():
        print cluster.cluster_tag
            print cluster"""
